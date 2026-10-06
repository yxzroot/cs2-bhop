"""Minimal, opt-in updater for the packaged DragiWare release.

The updater accepts one exact executable from the project's GitHub Releases
page. It never downloads source trees, packages, scripts, offsets, or ZIP
archives. A release asset must include GitHub's SHA-256 digest; assets without
one are deliberately ignored.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


OFFICIAL_REPOSITORY = "yxzroot/cs2-bhop"
RELEASES_URL = f"https://api.github.com/repos/{OFFICIAL_REPOSITORY}/releases"
RELEASE_ASSET_NAME = "DragiWare.exe"
USER_AGENT = "DragiWare-Updater/1.0"
REQUEST_TIMEOUT_SECONDS = 20
MAX_ASSET_SIZE = 200 * 1024 * 1024
MIN_ASSET_SIZE = 1024
VERSION_PATTERN = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
SHA256_PATTERN = re.compile(r"^sha256:([0-9a-fA-F]{64})$")


class UpdateError(RuntimeError):
    """Raised when an update cannot be safely checked, downloaded, or applied."""


@dataclass(frozen=True)
class StableRelease:
    version: str
    tag: str
    title: str
    asset_name: str
    asset_url: str
    asset_size: int
    sha256: str


def parse_version(value: str) -> tuple[int, int, int]:
    match = VERSION_PATTERN.fullmatch(str(value).strip())
    if not match:
        raise ValueError(f"Unsupported stable version: {value}")
    return tuple(int(part) for part in match.groups())


class UpdateManager:
    """Checks and applies explicitly approved, verified stable releases only."""

    def __init__(self, current_version: str, data_directory: Path, executable: Path):
        self.current_version = str(current_version).lstrip("v")
        self.data_directory = Path(data_directory)
        self.executable = Path(executable)
        self.update_directory = self.data_directory / "updates"

    def check_latest(self) -> StableRelease | None:
        """Return the newest eligible stable release, if it is newer than this build."""
        releases = self._request_json(f"{RELEASES_URL}?per_page=30")
        if not isinstance(releases, list):
            raise UpdateError("The official release service returned an invalid response.")

        candidates: list[tuple[tuple[int, int, int], StableRelease]] = []
        for entry in releases:
            if not isinstance(entry, dict) or entry.get("draft") or entry.get("prerelease"):
                continue
            release = self._release_from_entry(entry)
            if release is not None:
                candidates.append((parse_version(release.version), release))

        if not candidates:
            return None

        candidates.sort(key=lambda item: item[0], reverse=True)
        latest_version, latest = candidates[0]
        try:
            current_version = parse_version(self.current_version)
        except ValueError:
            # A development build may use a non-stable version suffix. It may
            # still opt in to the newest published stable release.
            current_version = (0, 0, 0)
        return latest if latest_version > current_version else None

    def download(self, release: StableRelease, progress=None) -> Path:
        """Download one declared release executable and verify its SHA-256 digest."""
        self._validate_release(release)
        self.update_directory.mkdir(parents=True, exist_ok=True)
        part = self.update_directory / f"DragiWare-{release.version}.exe.part"
        destination = self.update_directory / f"DragiWare-{release.version}.exe"
        part.unlink(missing_ok=True)

        request = urllib.request.Request(
            release.asset_url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/octet-stream"},
        )
        received = 0
        digest = hashlib.sha256()
        header = b""
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                content_length = response.headers.get("Content-Length")
                if not content_length:
                    raise UpdateError("The official release did not provide a download size.")
                try:
                    response_size = int(content_length)
                except ValueError as error:
                    raise UpdateError("The official release provided an invalid download size.") from error
                if response_size != release.asset_size:
                    raise UpdateError("The release metadata did not match the download size.")

                with part.open("xb") as output:
                    while True:
                        chunk = response.read(128 * 1024)
                        if not chunk:
                            break
                        if not header:
                            header = chunk[:2]
                        received += len(chunk)
                        if received > release.asset_size:
                            raise UpdateError("The download exceeded the declared release size.")
                        digest.update(chunk)
                        output.write(chunk)
                        if progress:
                            progress(received, release.asset_size)

            if received != release.asset_size:
                raise UpdateError("The update download ended before it was complete.")
            if header != b"MZ":
                raise UpdateError("The official update package was not a Windows executable.")
            if digest.hexdigest().lower() != release.sha256:
                raise UpdateError("The downloaded update failed SHA-256 verification.")
            part.replace(destination)
            return destination
        except (OSError, urllib.error.URLError, urllib.error.HTTPError) as error:
            raise UpdateError(f"Could not download the official update: {error}") from error
        finally:
            part.unlink(missing_ok=True)

    def install_after_exit(self, downloaded: Path, close_application) -> None:
        """Apply an already-verified executable after the user confirms restart.

        Windows cannot replace a running executable. This creates a fixed local
        helper with only validated local paths; it never executes the downloaded
        file as a script or accepts commands from a release asset.
        """
        source = Path(downloaded).resolve()
        update_root = self.update_directory.resolve()
        target = self.executable.resolve()
        if source.parent != update_root or source.suffix.lower() != ".exe":
            raise UpdateError("The selected update was not staged by DragiWare.")
        if target.name.lower() != RELEASE_ASSET_NAME.lower() or target.suffix.lower() != ".exe":
            raise UpdateError("Updates are supported only for the packaged DragiWare.exe release.")
        if not source.is_file() or source.stat().st_size < MIN_ASSET_SIZE:
            raise UpdateError("The verified update file is no longer available.")

        script = update_root / f"apply-dragiware-update-{os.getpid()}.cmd"
        script.write_text(
            self._installer_script(source, target, os.getpid(), script),
            encoding="utf-8",
            newline="\r\n",
        )
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        try:
            subprocess.Popen(
                ["cmd.exe", "/d", "/c", str(script)],
                creationflags=flags,
                close_fds=True,
            )
        except OSError as error:
            script.unlink(missing_ok=True)
            raise UpdateError(f"Could not start the local update helper: {error}") from error
        close_application()

    def _request_json(self, url: str):
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                return json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as error:
            raise UpdateError(f"Could not check the official release service: {error}") from error

    @staticmethod
    def _release_from_entry(entry: dict) -> StableRelease | None:
        tag = str(entry.get("tag_name") or "").strip()
        try:
            parse_version(tag)
        except ValueError:
            return None

        asset = next(
            (
                candidate
                for candidate in entry.get("assets", [])
                if isinstance(candidate, dict) and candidate.get("name") == RELEASE_ASSET_NAME
            ),
            None,
        )
        if asset is None:
            return None

        asset_url = str(asset.get("browser_download_url") or "")
        digest_match = SHA256_PATTERN.fullmatch(str(asset.get("digest") or ""))
        try:
            asset_size = int(asset.get("size") or 0)
        except (TypeError, ValueError):
            return None
        if not digest_match or not UpdateManager._is_official_asset_url(asset_url):
            return None
        if not MIN_ASSET_SIZE <= asset_size <= MAX_ASSET_SIZE:
            return None

        return StableRelease(
            version=tag.lstrip("v"),
            tag=tag,
            title=str(entry.get("name") or tag),
            asset_name=RELEASE_ASSET_NAME,
            asset_url=asset_url,
            asset_size=asset_size,
            sha256=digest_match.group(1).lower(),
        )

    @staticmethod
    def _is_official_asset_url(url: str) -> bool:
        parsed = urlparse(url)
        return (
            parsed.scheme == "https"
            and parsed.netloc == "github.com"
            and parsed.path.startswith(f"/{OFFICIAL_REPOSITORY}/releases/download/")
        )

    @staticmethod
    def _validate_release(release: StableRelease) -> None:
        if release.asset_name != RELEASE_ASSET_NAME:
            raise UpdateError("The selected release did not contain the expected DragiWare executable.")
        if not UpdateManager._is_official_asset_url(release.asset_url):
            raise UpdateError("The release asset was not hosted by the official release source.")
        if not MIN_ASSET_SIZE <= release.asset_size <= MAX_ASSET_SIZE:
            raise UpdateError("The release asset had an invalid size.")
        if not re.fullmatch(r"[0-9a-f]{64}", release.sha256):
            raise UpdateError("The release did not include a valid SHA-256 digest.")

    @staticmethod
    def _installer_script(source: Path, target: Path, process_id: int, script: Path) -> str:
        def quote(value: Path) -> str:
            text = str(value)
            if '"' in text or "\r" in text or "\n" in text:
                raise UpdateError("The update path contained unsupported characters.")
            return f'"{text}"'

        source_text = quote(source)
        target_text = quote(target)
        script_text = quote(script)
        target_staging_text = quote(target.with_suffix(".exe.new"))
        return (
            "@echo off\r\n"
            "setlocal DisableDelayedExpansion\r\n"
            f"set \"PID={process_id}\"\r\n"
            ":wait_for_dragiware\r\n"
            "tasklist /FI \"PID eq %PID%\" /NH | findstr /R /C:\" %PID% \" >nul\r\n"
            "if not errorlevel 1 (\r\n"
            "  timeout /t 1 /nobreak >nul\r\n"
            "  goto wait_for_dragiware\r\n"
            ")\r\n"
            f"copy /y {source_text} {target_staging_text} >nul\r\n"
            "if errorlevel 1 goto cleanup\r\n"
            f"move /y {target_staging_text} {target_text} >nul\r\n"
            "if errorlevel 1 goto cleanup\r\n"
            f"start \"DragiWare\" {target_text}\r\n"
            ":cleanup\r\n"
            f"del /q {source_text} >nul 2>&1\r\n"
            f"del /q {script_text} >nul 2>&1\r\n"
        )
