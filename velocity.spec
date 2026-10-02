# PyInstaller recipe for the native Velocity desktop application.
from PyInstaller.utils.hooks import collect_submodules


hiddenimports = collect_submodules("pymem")

a = Analysis(
    ["cs2_bhop.py"],
    pathex=[SPECPATH],
    binaries=[],
    datas=[("hello_kitty_background.png", ".")],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["PIL", "numpy", "pytest"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Velocity",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=True,
)
