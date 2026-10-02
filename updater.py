"""Velocity auto-updater — releases, news, and live CS2 offsets.

Source of truth for offsets: https://github.com/sezzyaep/CS2-OFFSETS

Four jobs, one file:

  1. Release channel    — finds the newest stable release for the project,
                          verifies it, downloads it, and stages a full-tree
                          swap that runs after shutdown.
  2. Post-swap rebuild  — after the tree mirror, every C++ translation unit
                          is recompiled with whichever toolchain is present
                          (MSVC cl → MinGW g++ → clang++).
  3. Offset intelligence— pulls fresh CS2 offsets from sezzyaep/CS2-OFFSETS,
                          rewrites the `offsets` namespace in cs2_bhop.cpp
                          and the OFFSET_* constants in bhop_core.py, and
                          never loses the user's originals (one-time .bak).
  4. News feed          — the ten most recent releases for the Updates page.

Standard library only. No sub-dependencies. Runs from source or frozen.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

# ==========================================================================
#  Configuration
# ==========================================================================

# --- Project release feed --------------------------------------------------
GITHUB_REPO = "yxzroot/bhop-script-"
GITHUB_API = f"https://api.github.com/repos/{GITHUB_REPO}"
USER_AGENT = "Velocity-Updater/1.0 (+https://github.com/yxzroot/bhop-script-)"
REQUEST_TIMEOUT = 30
DOWNLOAD_CHUNK = 64 * 1024
MAX_RELEASES = 30
NEWS_LIMIT = 10
CHECKSUM_SUFFIXES = (".sha256", ".sha256.txt", ".sha256sum", ".txt")

ARCHIVE_HINTS = ("velocity", "bhop", "source", "full")
EXECUTABLE_HINTS = ("velocity", "bhop")

# --- Offset source (the only one that matters) -----------------------------
OFFSETS_REPO = "sezzyaep/CS2-OFFSETS"
OFFSETS_BRANCH = "main"
OFFSETS_BRANCH_FALLBACK = "master"
OFFSETS_CACHE_TTL = 60 * 30  # 30 minutes

# --- Project file classification ------------------------------------------
PYTHON_SOURCES = (
    "cs2_bhop.py",
    "bhop_core.py",
    "themes.py",
    "updater.py",
    "build_config.py",
)

# Single C++ translation unit. There is no header — do not invent one.
CPP_SOURCES = ("cs2_bhop.cpp",)
CPP_EXTENSIONS = (".cpp", ".cc", ".cxx", ".c")
CPP_HEADER_EXTENSIONS = (".h", ".hpp", ".hxx")

# Files / dirs that must survive an update untouched.
PRESERVE_FILES = (
    "bhop_settings.json",
    "cs2_bhop.exe",
    "cs2_bhop.obj",
    "cs2_bhop.pdb",
)
PRESERVE_DIRS = (
    "bunnyhop logs",
    "dependencies",
    "updates",
    "__pycache__",
    ".git",
    "build",
    "offsets_cache",
)

# Offset mapping: what gets rewritten, and where it comes from upstream.
#
#   key       – internal name
#   cpp_name  – identifier inside `namespace offsets` in cs2_bhop.cpp (or None)
#   py_name   – OFFSET_* constant in bhop_core.py (or None)
#   source    – which upstream file / structure to read
#   lookup    – path inside the parsed upstream structure
OFFSET_MAP: dict = {
    "local_player_pawn": {
        "cpp_name": "dwLocalPlayerPawn",
        "py_name":  "OFFSET_LOCAL_PLAYER_PAWN",
        "source":   "offsets_json",
        "lookup":   ("client.dll", "dwLocalPlayerPawn"),
    },
    "force_jump": {
        "cpp_name": None,
        "py_name":  "OFFSET_FORCE_JUMP",
        "source":   "buttons_json",
        "lookup":   ("client.dll", "jump"),
    },
    "flags": {
        "cpp_name": "m_fFlags",
        "py_name":  "OFFSET_FLAGS",
        "source":   "client_dll_json",
        "lookup":   ("C_BaseEntity", "m_fFlags"),
    },
}

# Toolchain preference — first match wins.
TOOLCHAINS: Tuple[Tuple[str, List[str], List[str]], ...] = (
    (
        "MSVC (cl)",
        ["cl"],
        [
            "cl", "/nologo", "/EHsc", "/O2", "/std:c++17",
            "{src}", "/Fe:{out}",
            "/link", "user32.lib", "advapi32.lib",
            "/Fo:{obj_dir}\\",
        ],
    ),
    (
        "MinGW (g++)",
        ["g++"],
        [
            "g++", "-O2", "-std=c++17",
            "-o", "{out}", "{src}",
            "-luser32", "-ladvapi32",
            "-static-libgcc", "-static-libstdc++",
        ],
    ),
    (
        "Clang (clang++)",
        ["clang++"],
        [
            "clang++", "-O2", "-std=c++17",
            "-o", "{out}", "{src}",
            "-luser32", "-ladvapi32",
        ],
    ),
)


# ==========================================================================
#  Errors
# ==========================================================================

class UpdateError(RuntimeError):
    """Any update-pipeline problem the UI should surface verbatim."""


# ==========================================================================
#  Data models
# ==========================================================================

@dataclass(frozen=True)
class StableRelease:
    """A stable release entry with a downloadable asset."""

    version: str
    tag: str
    url: str
    sha256: str
    size: int
    notes: str
    published_at: str
    prerelease: bool = False

    @property
    def filename(self) -> str:
        tail = self.url.rsplit("/", 1)[-1]
        return tail.split("?", 1)[0].split("#", 1)[0] or "velocity-update.bin"

    @property
    def is_archive(self) -> bool:
        return self.filename.lower().endswith(".zip")

    @property
    def is_executable(self) -> bool:
        return self.filename.lower().endswith(".exe")


@dataclass(frozen=True)
class NewsItem:
    """One row for the official feed on the Updates page."""

    title: str
    version: str
    notes: str
    published_at: str
    url: str
    prerelease: bool


@dataclass
class BuildReport:
    """Result of a C++ rebuild pass."""

    ok: bool
    toolchain: str = ""
    compiled: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    log_path: Optional[Path] = None

    def summary(self) -> str:
        if not self.toolchain:
            return "no C++ toolchain detected — sources updated only"
        if self.ok and not self.errors:
            return f"{self.toolchain}: rebuilt {len(self.compiled)} source(s)"
        if self.errors:
            return f"{self.toolchain}: build failed ({len(self.errors)} error(s))"
        return f"{self.toolchain}: nothing to build"


@dataclass
class OffsetReport:
    """Result of a live offset refresh."""

    ok: bool
    values: dict = field(default_factory=dict)
    cpp_updates: int = 0
    py_updates: int = 0
    cpp_path: Optional[Path] = None
    py_path: Optional[Path] = None
    errors: List[str] = field(default_factory=list)

    def summary(self) -> str:
        if not self.ok:
            return "offset refresh failed — " + "; ".join(self.errors or ["unknown"])
        if not self.values:
            return "offset refresh returned no usable values"
        return (
            f"offsets refreshed — {len(self.values)} value(s), "
            f"{self.cpp_updates} C++ field(s), {self.py_updates} Python constant(s)"
        )


# ==========================================================================
#  Version helpers (lenient semver)
# ==========================================================================

_VERSION_RE = re.compile(
    r"(\d+)\s*\.\s*(\d+)\s*\.\s*(\d+)"
    r"(?:[-._]?\s*([0-9A-Za-z][0-9A-Za-z.\-]*))?"
)


def _parse_version(text: str) -> Tuple[int, int, int, str]:
    if not text:
        return (0, 0, 0, "")
    match = _VERSION_RE.search(str(text).strip().lstrip("vV"))
    if not match:
        return (0, 0, 0, "")
    major, minor, patch, suffix = match.groups()
    return (int(major), int(minor), int(patch), (suffix or "").lower())


def _is_newer(candidate: str, current: str) -> bool:
    return _parse_version(candidate) > _parse_version(current)


# ==========================================================================
#  UpdateManager — releases, download, swap, rebuild
# ==========================================================================

class UpdateManager:
    """Fetches releases, downloads archives, and stages a full-tree swap.

    After the tree swap the batch script also runs a build pass over every
    C++ translation unit it finds in the new tree, so a source update always
    produces a matching compiled binary.
    """

    def __init__(self, current_version: str, data_directory: Path, executable: Path) -> None:
        self.current_version = str(current_version or "0.0.0")
        self.data_directory = Path(data_directory)
        self.data_directory.mkdir(parents=True, exist_ok=True)
        self.update_directory = self.data_directory / "updates"
        self.update_directory.mkdir(parents=True, exist_ok=True)
        self.executable = Path(executable)
        self._lock = threading.Lock()
        self._manifest_warned = False
        self.last_build_report: Optional[BuildReport] = None

    # ==================================================================
    #  Networking
    # ==================================================================

    def _open(self, url: str, accept: str = "application/vnd.github+json"):
        request = urllib.request.Request(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": accept},
        )
        try:
            return urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT)
        except urllib.error.HTTPError as exc:
            raise UpdateError(f"Server returned HTTP {exc.code} for {url}") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise UpdateError(f"Could not reach the update server: {reason}") from exc

    def _request_json(self, url: str):
        with self._open(url) as response:
            payload = response.read().decode("utf-8", errors="replace")
        try:
            return json.loads(payload)
        except json.JSONDecodeError as exc:
            raise UpdateError(f"Malformed response from update server: {exc}") from exc

    def _request_text(self, url: str) -> str:
        with self._open(url, accept="text/plain, */*") as response:
            return response.read().decode("utf-8", errors="replace")

    # ==================================================================
    #  Release parsing
    # ==================================================================

    @staticmethod
    def _select_release_asset(assets: Sequence[dict]) -> Optional[dict]:
        zips = [
            a for a in assets
            if isinstance(a, dict) and (a.get("name") or "").lower().endswith(".zip")
        ]
        if zips:
            for asset in zips:
                name = (asset.get("name") or "").lower()
                if any(hint in name for hint in ARCHIVE_HINTS):
                    return asset
            return zips[0]
        exes = [
            a for a in assets
            if isinstance(a, dict) and (a.get("name") or "").lower().endswith(".exe")
        ]
        if exes:
            for asset in exes:
                name = (asset.get("name") or "").lower()
                if any(hint in name for hint in EXECUTABLE_HINTS):
                    return asset
            return exes[0]
        return None

    def _extract_sha256(self, asset: dict, assets: Sequence[dict]) -> str:
        digest = asset.get("digest")
        if isinstance(digest, str) and digest.startswith("sha256:"):
            token = digest.split(":", 1)[1].strip().lower()
            if re.fullmatch(r"[0-9a-f]{64}", token):
                return token

        asset_stem = Path((asset.get("name") or "").lower()).stem
        for candidate in assets:
            if not isinstance(candidate, dict):
                continue
            name = (candidate.get("name") or "").lower()
            if not name.endswith(CHECKSUM_SUFFIXES):
                continue
            if asset_stem and asset_stem not in Path(name).stem:
                continue
            url = candidate.get("browser_download_url")
            if not url:
                continue
            try:
                text = self._request_text(url)
            except UpdateError:
                continue
            for line in text.splitlines():
                token = line.strip().split(" ")[0].lstrip("*").lower()
                if re.fullmatch(r"[0-9a-f]{64}", token):
                    return token
        return ""

    def _release_from_entry(self, entry: dict) -> Optional[StableRelease]:
        assets = entry.get("assets") or []
        if not isinstance(assets, list):
            assets = []
        asset = self._select_release_asset(assets)
        if not asset:
            return None
        url = asset.get("browser_download_url")
        if not url:
            return None
        tag = (entry.get("tag_name") or entry.get("name") or "").strip()
        version = tag.lstrip("vV").strip() or "0.0.0"
        return StableRelease(
            version=version,
            tag=tag or f"v{version}",
            url=url,
            sha256=self._extract_sha256(asset, assets),
            size=int(asset.get("size") or 0),
            notes=entry.get("body") or "",
            published_at=entry.get("published_at") or "",
            prerelease=bool(entry.get("prerelease")),
        )

    # ==================================================================
    #  Public API — matches the desktop controller
    # ==================================================================

    def check_latest(self) -> Optional[StableRelease]:
        data = self._request_json(f"{GITHUB_API}/releases?per_page={MAX_RELEASES}")
        if not isinstance(data, list):
            raise UpdateError("The release feed did not return a list of releases.")
        with self._lock:
            best: Optional[StableRelease] = None
            for entry in data:
                if not isinstance(entry, dict) or entry.get("draft") or entry.get("prerelease"):
                    continue
                release = self._release_from_entry(entry)
                if release is None:
                    continue
                if not _is_newer(release.version, self.current_version):
                    continue
                if best is None or _is_newer(release.version, best.version):
                    best = release
            return best

    def fetch_news(self) -> List[NewsItem]:
        data = self._request_json(f"{GITHUB_API}/releases?per_page={NEWS_LIMIT}")
        if not isinstance(data, list):
            raise UpdateError("The news feed did not return a list of releases.")
        items: List[NewsItem] = []
        for entry in data:
            if not isinstance(entry, dict) or entry.get("draft"):
                continue
            tag = (entry.get("tag_name") or "").strip()
            version = tag.lstrip("vV").strip() or "unversioned"
            items.append(NewsItem(
                title=entry.get("name") or tag or "Velocity release",
                version=version,
                notes=entry.get("body") or "",
                published_at=entry.get("published_at") or "",
                url=entry.get("html_url") or "",
                prerelease=bool(entry.get("prerelease")),
            ))
        return items

    def download(
        self,
        release: StableRelease,
        progress: Optional[Callable[[int, int], None]] = None,
    ) -> Path:
        """Download and verify the release asset; return the cache path."""
        if not release.url:
            raise UpdateError("Release does not include a download URL.")

        cache_root = self.update_directory / release.version.replace("/", "_")
        cache_root.mkdir(parents=True, exist_ok=True)
        archive_path = cache_root / release.filename

        if release.is_archive:
            extracted = cache_root / "project"
            if (
                archive_path.exists()
                and release.sha256
                and self._verify_file(archive_path, release.sha256)
            ):
                if not self._is_populated(extracted):
                    self._extract_archive(archive_path, extracted)
                tree = self._flatten_single_root(extracted)
                self._verify_manifest(tree)
                return tree
            self._fetch_asset(release, archive_path, progress)
            if extracted.exists():
                shutil.rmtree(extracted, ignore_errors=True)
            self._extract_archive(archive_path, extracted)
            tree = self._flatten_single_root(extracted)
            self._verify_manifest(tree)
            return tree

        # Single-file .exe path
        if (
            archive_path.exists()
            and release.sha256
            and self._verify_file(archive_path, release.sha256)
        ):
            if progress:
                size = archive_path.stat().st_size
                self._safe_progress(progress, size, size)
            return archive_path
        self._fetch_asset(release, archive_path, progress)
        return archive_path

    def stage_and_install_on_exit(
        self,
        downloaded: Path,
        shutdown_callback: Callable[[], None],
    ) -> Path:
        """Stage a swap-and-rebuild and hand off to shutdown."""
        downloaded = Path(downloaded)
        frozen = bool(getattr(sys, "frozen", False))

        if downloaded.is_dir():
            target = self.executable.parent
            source = self._locate_source_tree(downloaded, frozen)
            cpp_sources = self._discover_cpp_sources(source)
            script = self._write_project_swap(
                target=target,
                source=source,
                launch=self._launcher_command(frozen),
                cpp_sources=cpp_sources,
            )
            self._launch_detached(script)
            shutdown_callback()
            return script

        if downloaded.suffix.lower() == ".exe":
            if not frozen:
                raise UpdateError("Single-file updates require the packaged Velocity.exe build.")
            script = self._write_exe_swap(
                Path(sys.executable).resolve(), downloaded.resolve()
            )
            self._launch_detached(script)
            shutdown_callback()
            return script

        raise UpdateError(f"Unsupported update payload: {downloaded.name}")

    # ------------------------------------------------------------------
    #  Synchronous C++ rebuild (GUI-callable)
    # ------------------------------------------------------------------

    def rebuild_cpp(
        self,
        project_root: Optional[Path] = None,
        log_sink: Optional[Callable[[str], None]] = None,
    ) -> BuildReport:
        """Compile every C++ translation unit in the project tree."""
        root = Path(project_root) if project_root else self.executable.parent
        log_lines: List[str] = []

        def log(message: str) -> None:
            log_lines.append(message)
            if log_sink:
                try:
                    log_sink(message)
                except Exception:
                    pass

        report = BuildReport(ok=False)

        toolchain = self._detect_toolchain()
        if toolchain is None:
            log("No C++ compiler found on PATH (tried cl, g++, clang++).")
            report.errors.append("no toolchain")
            return report

        label, _probe, template = toolchain
        report.toolchain = label
        log(f"Toolchain: {label}")

        sources = self._discover_cpp_sources(root)
        if not sources:
            log("No C++ sources found in project tree.")
            report.ok = True
            return report

        obj_dir = root / "build" / "obj"
        obj_dir.mkdir(parents=True, exist_ok=True)
        out_exe = root / "cs2_bhop.exe"
        staging_exe = root / ".cs2_bhop.exe.new"
        staging_exe.unlink(missing_ok=True)

        for src in sources:
            if src.name.startswith("."):
                report.skipped.append(str(src.relative_to(root)))
                continue
            log(f"Compiling {src.name}")
            argv = self._compile_command(template, src, staging_exe, obj_dir)
            try:
                proc = subprocess.run(
                    argv,
                    cwd=str(root),
                    capture_output=True,
                    text=True,
                    timeout=180,
                )
            except FileNotFoundError:
                report.errors.append(f"{src.name}: compiler not found")
                continue
            except subprocess.TimeoutExpired:
                report.errors.append(f"{src.name}: compile timed out")
                continue

            if proc.stdout.strip():
                log(proc.stdout.rstrip())
            if proc.stderr.strip():
                log(proc.stderr.rstrip())

            if proc.returncode != 0:
                report.errors.append(f"{src.name}: exit code {proc.returncode}")
                continue
            report.compiled.append(str(src.relative_to(root)))

        if report.compiled and not report.errors and staging_exe.exists():
            try:
                out_exe.unlink(missing_ok=True)
                staging_exe.replace(out_exe)
                log(f"Installed new binary: {out_exe.name}")
            except OSError as exc:
                report.errors.append(f"install failed: {exc}")
        elif staging_exe.exists():
            staging_exe.unlink(missing_ok=True)

        report.ok = not report.errors
        log_path = self.update_directory / "build.log"
        try:
            log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
            report.log_path = log_path
        except OSError:
            pass
        return report

    # ------------------------------------------------------------------
    #  Cache maintenance
    # ------------------------------------------------------------------

    def purge_cache(self, keep: Optional[Path] = None) -> None:
        keep_resolved = Path(keep).resolve() if keep else None
        for entry in self.update_directory.iterdir():
            try:
                resolved = entry.resolve()
            except OSError:
                continue
            if keep_resolved and (
                resolved == keep_resolved or str(keep_resolved).startswith(str(resolved))
            ):
                continue
            try:
                if entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink(missing_ok=True)
            except OSError:
                continue

    # ==================================================================
    #  Download / extract internals
    # ==================================================================

    def _fetch_asset(
        self,
        release: StableRelease,
        target: Path,
        progress: Optional[Callable[[int, int], None]],
    ) -> None:
        part = target.with_name(target.name + ".part")
        part.unlink(missing_ok=True)
        digest = hashlib.sha256()
        received = 0
        try:
            with self._open(release.url, accept="application/octet-stream, */*") as response:
                declared = response.headers.get("Content-Length") or "0"
                try:
                    total = int(declared)
                except (TypeError, ValueError):
                    total = int(release.size or 0)
                with part.open("wb") as handle:
                    while True:
                        chunk = response.read(DOWNLOAD_CHUNK)
                        if not chunk:
                            break
                        digest.update(chunk)
                        received += len(chunk)
                        handle.write(chunk)
                        if progress:
                            self._safe_progress(progress, received, total)
        except UpdateError:
            part.unlink(missing_ok=True)
            raise
        except OSError as exc:
            part.unlink(missing_ok=True)
            raise UpdateError(f"Could not write the update to disk: {exc}") from exc

        if release.sha256 and digest.hexdigest().lower() != release.sha256.lower():
            part.unlink(missing_ok=True)
            raise UpdateError("Downloaded update failed checksum verification.")
        if not release.sha256 and release.size and received != release.size:
            part.unlink(missing_ok=True)
            raise UpdateError("Downloaded update size did not match the release metadata.")

        target.unlink(missing_ok=True)
        part.replace(target)

    def _extract_archive(self, archive_path: Path, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=True)
        root = destination.resolve()
        try:
            with zipfile.ZipFile(archive_path) as archive:
                for member in archive.infolist():
                    name = member.filename
                    if not name:
                        continue
                    member_path = (destination / name).resolve()
                    try:
                        member_path.relative_to(root)
                    except ValueError as exc:
                        raise UpdateError(f"Archive contains an unsafe path: {name}") from exc
                    if member.is_dir():
                        member_path.mkdir(parents=True, exist_ok=True)
                        continue
                    member_path.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(member) as src, member_path.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
        except zipfile.BadZipFile as exc:
            raise UpdateError(f"Downloaded archive is corrupt: {exc}") from exc

    @staticmethod
    def _flatten_single_root(directory: Path) -> Path:
        children = [p for p in directory.iterdir() if not p.name.startswith("__MACOSX")]
        if len(children) == 1 and children[0].is_dir():
            return children[0]
        return directory

    @staticmethod
    def _is_populated(directory: Path) -> bool:
        try:
            return directory.is_dir() and any(directory.iterdir())
        except OSError:
            return False

    @staticmethod
    def _locate_source_tree(payload: Path, frozen: bool) -> Path:
        if not frozen:
            return payload
        for entry in payload.rglob("*.exe"):
            return entry.parent
        raise UpdateError("Archive did not contain an executable for this frozen build.")

    @staticmethod
    def _safe_progress(callback: Callable[[int, int], None], received: int, total: int) -> None:
        try:
            callback(received, total)
        except Exception:
            pass

    def _verify_file(self, path: Path, expected: str) -> bool:
        if not expected:
            return False
        try:
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(DOWNLOAD_CHUNK), b""):
                    digest.update(chunk)
            return digest.hexdigest().lower() == expected.lower()
        except OSError:
            return False

    # ==================================================================
    #  Manifest / C++ helpers
    # ==================================================================

    def _verify_manifest(self, tree: Path) -> None:
        """Non-fatal sanity check that a downloaded tree has the pieces we expect."""
        missing: List[str] = []
        for name in PYTHON_SOURCES:
            if not (tree / name).exists():
                missing.append(name)
        if missing and not self._manifest_warned:
            self._manifest_warned = True
            try:
                (self.update_directory / "manifest.log").write_text(
                    "Missing expected project files:\n" + "\n".join(missing) + "\n",
                    encoding="utf-8",
                )
            except OSError:
                pass

    @staticmethod
    def _discover_cpp_sources(root: Path) -> List[Path]:
        found: List[Path] = []
        if not root.is_dir():
            return found
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.name.startswith("."):
                continue
            if any(part in ("build", "__pycache__", "updates", "dependencies", "offsets_cache")
                   for part in path.parts):
                continue
            suffix = path.suffix.lower()
            if suffix in CPP_EXTENSIONS:
                found.append(path)
        found.sort(key=lambda p: (p.name != "cs2_bhop.cpp", str(p).lower()))
        return found

    @staticmethod
    def _detect_toolchain() -> Optional[Tuple[str, List[str], List[str]]]:
        for entry in TOOLCHAINS:
            label, probe, template = entry
            if shutil.which(probe[0]):
                return label, probe, template
        return None

    @staticmethod
    def _compile_command(
        template: Sequence[str],
        source: Path,
        output: Path,
        obj_dir: Path,
    ) -> List[str]:
        return [
            part.format(src=str(source), out=str(output), obj_dir=str(obj_dir))
            for part in template
        ]

    # ==================================================================
    #  Swap scripts
    # ==================================================================

    @staticmethod
    def _launcher_command(frozen: bool) -> str:
        if frozen:
            return 'start "" "%PROJECT%\\Velocity.exe"'
        return 'start "Velocity V2" /b pythonw "%PROJECT%\\cs2_bhop.py"'

    def _write_project_swap(
        self,
        target: Path,
        source: Path,
        launch: str,
        cpp_sources: Sequence[Path],
    ) -> Path:
        script = self.update_directory / "apply_update.bat"
        log = self.update_directory / "apply_update.log"
        build_log = self.update_directory / "build.log"
        pid = os.getpid()

        excludes_file = " ".join(f'"{name}"' for name in PRESERVE_FILES) or '""'
        excludes_dir = " ".join(f'"{name}"' for name in PRESERVE_DIRS) or '""'

        try:
            rel_sources = [str(p.relative_to(source)) for p in cpp_sources]
        except ValueError:
            rel_sources = [str(p.name) for p in cpp_sources]
        src_list = " ".join(f'"{name}"' for name in rel_sources) or '""'

        toolchain_block = (
            "set \"BUILT=0\"\r\n"
            "where cl >NUL 2>&1\r\n"
            "if not errorlevel 1 ( set \"TOOLCHAIN=cl\" & goto build )\r\n"
            "where g++ >NUL 2>&1\r\n"
            "if not errorlevel 1 ( set \"TOOLCHAIN=g++\" & goto build )\r\n"
            "where clang++ >NUL 2>&1\r\n"
            "if not errorlevel 1 ( set \"TOOLCHAIN=clang++\" & goto build )\r\n"
            'echo [%date% %time%] no C++ compiler on PATH; skipping rebuild >>"%BUILDLOG%"\r\n'
            "goto afterbuild\r\n"
            ":build\r\n"
            'echo [%date% %time%] toolchain=%TOOLCHAIN% >>"%BUILDLOG%"\r\n'
            'if not exist "build\\obj" mkdir "build\\obj"\r\n'
            f'for %%S in ({src_list}) do (\r\n'
            '    if "%%~S"=="" goto afterloop\r\n'
            '    echo [%date% %time%] compiling %%S >>"%BUILDLOG%"\r\n'
            '    if "%TOOLCHAIN%"=="cl" (\r\n'
            '        cl /nologo /EHsc /O2 /std:c++17 "%%S" /Fe:".cs2_bhop.exe.new" /Fo:"build\\obj\\" /link user32.lib advapi32.lib >>"%BUILDLOG%" 2>&1\r\n'
            '    ) else if "%TOOLCHAIN%"=="g++" (\r\n'
            '        g++ -O2 -std=c++17 -o ".cs2_bhop.exe.new" "%%S" -luser32 -ladvapi32 -static-libgcc -static-libstdc++ >>"%BUILDLOG%" 2>&1\r\n'
            '    ) else (\r\n'
            '        clang++ -O2 -std=c++17 -o ".cs2_bhop.exe.new" "%%S" -luser32 -ladvapi32 >>"%BUILDLOG%" 2>&1\r\n'
            '    )\r\n'
            '    if errorlevel 1 (\r\n'
            '        echo [%date% %time%] compile failed for %%S >>"%BUILDLOG%"\r\n'
            '        del ".cs2_bhop.exe.new" 2>NUL\r\n'
            '    ) else (\r\n'
            '        set "BUILT=1"\r\n'
            '    )\r\n'
            ')\r\n'
            ":afterloop\r\n"
            'if "%BUILT%"=="1" (\r\n'
            '    if exist ".cs2_bhop.exe.new" (\r\n'
            '        move /Y ".cs2_bhop.exe.new" "cs2_bhop.exe" >>"%BUILDLOG%" 2>&1\r\n'
            '        echo [%date% %time%] installed fresh cs2_bhop.exe >>"%BUILDLOG%"\r\n'
            '    )\r\n'
            ')\r\n'
            ":afterbuild\r\n"
        )

        template = (
            "@echo off\r\n"
            "setlocal enableextensions enabledelayedexpansion\r\n"
            f'set "TARGET={target}"\r\n'
            f'set "SOURCE={source}"\r\n'
            f'set "TARGETPID={pid}"\r\n'
            f'set "LOGFILE={log}"\r\n'
            f'set "BUILDLOG={build_log}"\r\n'
            'set "PROJECT=%TARGET%"\r\n'
            'echo [%date% %time%] update stage started >>"%LOGFILE%"\r\n'
            ":waitloop\r\n"
            'tasklist /FI "PID eq %TARGETPID%" 2>NUL | findstr /R /C:"[ ]%TARGETPID%[ ]" >NUL\r\n'
            "if not errorlevel 1 (\r\n"
            "    ping -n 2 127.0.0.1 >NUL\r\n"
            "    goto waitloop\r\n"
            ")\r\n"
            'echo [%date% %time%] process exited; mirroring tree >>"%LOGFILE%"\r\n'
            f'robocopy "%SOURCE%" "%TARGET%" /E /XF {excludes_file} /XD {excludes_dir} >>"%LOGFILE%" 2>&1\r\n'
            "set RC=%ERRORLEVEL%\r\n"
            "if %RC% GEQ 8 (\r\n"
            '    echo [%date% %time%] robocopy failed: %RC% >>"%LOGFILE%"\r\n'
            "    exit /b 1\r\n"
            ")\r\n"
            "cd /d \"%TARGET%\"\r\n"
            + toolchain_block +
            'if exist "%TARGET%\\requirements.txt" (\r\n'
            '    echo [%date% %time%] refreshing python dependencies >>"%LOGFILE%"\r\n'
            '    python -m pip install --upgrade -r requirements.txt --disable-pip-version-check >>"%LOGFILE%" 2>&1\r\n'
            ")\r\n"
            f"{launch}\r\n"
            'del "%~f0"\r\n'
            "exit /b 0\r\n"
        )
        script.write_text(template, encoding="utf-8")
        return script

    def _write_exe_swap(self, target: Path, source: Path) -> Path:
        script = self.update_directory / "apply_update.bat"
        log = self.update_directory / "apply_update.log"
        pid = os.getpid()
        template = (
            "@echo off\r\n"
            "setlocal enableextensions\r\n"
            f'set "TARGET={target}"\r\n'
            f'set "SOURCE={source}"\r\n'
            f'set "TARGETPID={pid}"\r\n'
            f'set "LOGFILE={log}"\r\n'
            ":waitloop\r\n"
            'tasklist /FI "PID eq %TARGETPID%" 2>NUL | findstr /R /C:"[ ]%TARGETPID%[ ]" >NUL\r\n'
            "if not errorlevel 1 (\r\n"
            "    ping -n 2 127.0.0.1 >NUL\r\n"
            "    goto waitloop\r\n"
            ")\r\n"
            'move /Y "%SOURCE%" "%TARGET%" >>"%LOGFILE%" 2>&1\r\n'
            "if errorlevel 1 (\r\n"
            '    echo [%date% %time%] swap failed >>"%LOGFILE%"\r\n'
            "    exit /b 1\r\n"
            ")\r\n"
            'start "" "%TARGET%"\r\n'
            'del "%~f0"\r\n'
            "exit /b 0\r\n"
        )
        script.write_text(template, encoding="utf-8")
        return script

    @staticmethod
    def _launch_detached(script: Path) -> None:
        creation = 0
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            creation |= subprocess.CREATE_NO_WINDOW
        if hasattr(subprocess, "DETACHED_PROCESS"):
            creation |= subprocess.DETACHED_PROCESS
        try:
            subprocess.Popen(
                ["cmd.exe", "/c", str(script)],
                creationflags=creation,
                close_fds=True,
            )
        except OSError as exc:
            raise UpdateError(f"Could not launch the update script: {exc}") from exc


# ==========================================================================
#  OffsetFetcher — live CS2 offsets from sezzyaep/CS2-OFFSETS
# ==========================================================================

class OffsetFetcher:
    """Downloads fresh CS2 offsets from sezzyaep/CS2-OFFSETS and rewrites
    them into the project.

    Upstream is the only source. We read:

      * offsets.json       — module-relative offsets (dwLocalPlayerPawn)
      * buttons.json       — key-state offsets (jump → dwForceJump)
      * client_dll.json    — class fields (m_fFlags inside C_BaseEntity)

    Nothing hits the network unless `fetch()` or `refresh_project()` runs.
    Every rewrite backs up the original to `<name>.bak` exactly once.
    """

    def __init__(self, data_directory: Path) -> None:
        self.data_directory = Path(data_directory)
        self.cache_dir = self.data_directory / "offsets_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._branch: Optional[str] = None
        self._user_agent = "Velocity-Offsets/1.0"

    # ------------------------------------------------------------------
    #  Public API
    # ------------------------------------------------------------------

    def refresh_project(
        self,
        project_root: Optional[Path] = None,
        force: bool = False,
    ) -> OffsetReport:
        """Fetch, apply, and return a full report.

        Never raises — failures land in `.errors`. Callers that want the
        raw dict should use `fetch()`.
        """
        root = Path(project_root) if project_root else Path.cwd()
        report = OffsetReport(ok=False)
        try:
            values = self.fetch(force=force)
        except UpdateError as exc:
            report.errors.append(str(exc))
            return report

        report.values = values

        cpp_path = root / "cs2_bhop.cpp"
        py_path = root / "bhop_core.py"
        if cpp_path.is_file():
            try:
                report.cpp_updates = self.apply_to_cpp(cpp_path, values)
                report.cpp_path = cpp_path
            except UpdateError as exc:
                report.errors.append(f"cpp: {exc}")
        else:
            report.errors.append(f"cpp: {cpp_path.name} not found")

        if py_path.is_file():
            try:
                report.py_updates = self.apply_to_python(py_path, values)
                report.py_path = py_path
            except UpdateError as exc:
                report.errors.append(f"py: {exc}")
        else:
            report.errors.append(f"py: {py_path.name} not found")

        report.ok = not report.errors
        return report

    def fetch(self, force: bool = False) -> dict:
        """Return {'local_player_pawn': 0x..., 'force_jump': 0x..., ...}."""
        if not force:
            cached = self._read_cache()
            if cached:
                return cached

        errors: List[str] = []
        merged: dict = {}

        # offsets.json — dwLocalPlayerPawn
        raw = self._raw("offsets.json")
        if raw:
            merged.update(self._parse_offsets_json(raw, errors))
        else:
            errors.append("offsets.json unreachable")

        # buttons.json — jump (dwForceJump)
        raw = self._raw("buttons.json")
        if raw:
            merged.update(self._parse_buttons_json(raw, errors))
        else:
            errors.append("buttons.json unreachable")

        # client_dll.json — m_fFlags inside C_BaseEntity
        raw = self._raw("client_dll.json")
        if raw:
            merged.update(self._parse_client_dll_json(raw, errors))
        else:
            errors.append("client_dll.json unreachable")

        wanted = {"local_player_pawn", "force_jump", "flags"}
        missing = wanted - merged.keys()
        if missing:
            raise UpdateError(
                "Offset fetch incomplete — missing: "
                + ", ".join(sorted(missing))
                + (f"  ({'; '.join(errors)})" if errors else "")
            )

        self._write_cache(merged)
        return merged

    def apply_to_cpp(self, cpp_path: Path, offsets: dict) -> int:
        """Rewrite the `offsets` namespace inside cs2_bhop.cpp in place."""
        path = Path(cpp_path)
        if not path.is_file():
            raise UpdateError(f"{path.name} not found at {path}")
        text = path.read_text(encoding="utf-8", errors="replace")
        updated = 0

        for key, spec in OFFSET_MAP.items():
            cpp_name = spec.get("cpp_name")
            if not cpp_name or key not in offsets:
                continue
            new_hex = f"0x{offsets[key]:X}"
            pattern = re.compile(
                r"(constexpr\s+std::ptrdiff_t\s+"
                + re.escape(cpp_name)
                + r"\s*=\s*)(0x[0-9A-Fa-f]+)",
                re.MULTILINE,
            )

            def _sub(match, replacement=new_hex):
                return match.group(1) + replacement

            new_text, count = pattern.subn(_sub, text)
            if count and new_text != text:
                updated += 1
                text = new_text

        if updated:
            self._backup_once(path)
            path.write_text(text, encoding="utf-8")
        return updated

    def apply_to_python(self, py_path: Path, offsets: dict) -> int:
        """Rewrite OFFSET_* constants inside bhop_core.py in place."""
        path = Path(py_path)
        if not path.is_file():
            raise UpdateError(f"{path.name} not found at {path}")
        text = path.read_text(encoding="utf-8", errors="replace")
        updated = 0

        for key, spec in OFFSET_MAP.items():
            py_name = spec.get("py_name")
            if not py_name or key not in offsets:
                continue
            new_hex = f"0x{offsets[key]:X}"
            pattern = re.compile(
                r"^(" + re.escape(py_name) + r"\s*=\s*)0x[0-9A-Fa-f]+",
                re.MULTILINE,
            )

            def _sub(match, replacement=new_hex):
                return match.group(1) + replacement

            new_text, count = pattern.subn(_sub, text)
            if count and new_text != text:
                updated += 1
                text = new_text

        if updated:
            self._backup_once(path)
            path.write_text(text, encoding="utf-8")
        return updated

    def purge_cache(self) -> None:
        for child in self.cache_dir.iterdir():
            try:
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
            except OSError:
                continue

    # ------------------------------------------------------------------
    #  Branch resolution / raw fetching
    # ------------------------------------------------------------------

    def _resolve_branch(self) -> str:
        if self._branch:
            return self._branch
        for candidate in (OFFSETS_BRANCH, OFFSETS_BRANCH_FALLBACK):
            url = f"https://raw.githubusercontent.com/{OFFSETS_REPO}/{candidate}/readme.md"
            try:
                request = urllib.request.Request(
                    url, headers={"User-Agent": self._user_agent}
                )
                with urllib.request.urlopen(request, timeout=10) as response:
                    response.read(1)
                self._branch = candidate
                return candidate
            except (urllib.error.URLError, urllib.error.HTTPError, OSError):
                continue
        self._branch = OFFSETS_BRANCH
        return self._branch

    def _raw(self, filename: str) -> Optional[str]:
        branch = self._resolve_branch()
        url = f"https://raw.githubusercontent.com/{OFFSETS_REPO}/{branch}/{filename}"
        try:
            request = urllib.request.Request(url, headers={"User-Agent": self._user_agent})
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, urllib.error.HTTPError, OSError):
            return None

    # ------------------------------------------------------------------
    #  Parsers — one per upstream file
    # ------------------------------------------------------------------

    @staticmethod
    def _coerce_int(value) -> Optional[int]:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            try:
                return int(value, 0)
            except ValueError:
                return None
        return None

    def _parse_offsets_json(self, raw: str, errors: List[str]) -> dict:
        """Read offsets.json → {'local_player_pawn': 0x...}."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            errors.append(f"offsets.json: {exc}")
            return {}
        if not isinstance(data, dict):
            errors.append("offsets.json: root is not an object")
            return {}

        out: dict = {}
        for key, spec in OFFSET_MAP.items():
            if spec["source"] != "offsets_json":
                continue
            module, field = spec["lookup"]
            module_data = data.get(module)
            if not isinstance(module_data, dict):
                continue
            value = self._coerce_int(module_data.get(field))
            if value is not None:
                out[key] = value
        return out

    def _parse_buttons_json(self, raw: str, errors: List[str]) -> dict:
        """Read buttons.json → {'force_jump': 0x...}."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            errors.append(f"buttons.json: {exc}")
            return {}
        if not isinstance(data, dict):
            errors.append("buttons.json: root is not an object")
            return {}

        out: dict = {}
        for key, spec in OFFSET_MAP.items():
            if spec["source"] != "buttons_json":
                continue
            module, field = spec["lookup"]
            module_data = data.get(module)
            if not isinstance(module_data, dict):
                continue
            value = self._coerce_int(module_data.get(field))
            if value is not None:
                out[key] = value
        return out

    def _parse_client_dll_json(self, raw: str, errors: List[str]) -> dict:
        """Read client_dll.json → {'flags': 0x...} from C_BaseEntity.m_fFlags.

        The dump nests classes under ``client.dll.classes``. We walk the
        known class name and pull the field offset. If the class or field
        is absent, the value is left untouched rather than written wrong.
        """
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            errors.append(f"client_dll.json: {exc}")
            return {}
        if not isinstance(data, dict):
            errors.append("client_dll.json: root is not an object")
            return {}

        out: dict = {}
        for key, spec in OFFSET_MAP.items():
            if spec["source"] != "client_dll_json":
                continue
            class_name, field_name = spec["lookup"]
            # Path: {"client.dll": {"classes": {"C_BaseEntity": {"fields": {...}}}}}
            module = data.get("client.dll")
            if not isinstance(module, dict):
                continue
            classes = module.get("classes")
            if not isinstance(classes, dict):
                continue
            class_data = classes.get(class_name)
            if not isinstance(class_data, dict):
                continue
            fields = class_data.get("fields")
            if not isinstance(fields, dict):
                continue
            field_data = fields.get(field_name)
            if not isinstance(field_data, dict):
                continue
            value = self._coerce_int(field_data.get("offset"))
            if value is not None:
                out[key] = value
        return out

    # ------------------------------------------------------------------
    #  Cache
    # ------------------------------------------------------------------

    def _cache_path(self) -> Path:
        return self.cache_dir / "latest.json"

    def _read_cache(self) -> Optional[dict]:
        path = self._cache_path()
        if not path.is_file():
            return None
        try:
            age = time.time() - path.stat().st_mtime
            if age > OFFSETS_CACHE_TTL:
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        out: dict = {}
        for key, value in data.items():
            coerced = self._coerce_int(value)
            if coerced is not None:
                out[key] = coerced
        return out or None

    def _write_cache(self, offsets: dict) -> None:
        try:
            self._cache_path().write_text(
                json.dumps(offsets, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass

    @staticmethod
    def _backup_once(path: Path) -> None:
        backup = path.with_suffix(path.suffix + ".bak")
        if backup.exists():
            return
        try:
            shutil.copy2(path, backup)
        except OSError:
            pass