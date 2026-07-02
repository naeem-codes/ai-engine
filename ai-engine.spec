# PyInstaller spec - bundles the Lumi Design AI Engine into a single ai-engine.exe
# (embeds Python + uvicorn + FastAPI + all deps so the client needs no Python install).
#
# Build:  python -m PyInstaller ai-engine.spec --noconfirm
# Output: dist/ai-engine.exe
#
# main.py is already frozen-aware: when running as the exe it resolves .env and
# rules/ next to the exe (sys.executable's folder), so no data files are embedded
# here - they live beside the exe and stay editable on the client machine.

from PyInstaller.utils.hooks import collect_submodules

# uvicorn picks its event-loop / http / websocket implementations at runtime via
# importlib, so PyInstaller's static analysis can't see them. Collect them explicitly.
hiddenimports = (
    collect_submodules("uvicorn")
    + ["anyio", "anyio._backends._asyncio"]
)

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib"],  # not used; trims size
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ai-engine",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,           # visible console so engine startup / API-key errors are diagnosable
    disable_windowed_traceback=False,
)
# Note: passing a.binaries + a.datas into EXE() above with no COLLECT step
# produces a single-file ai-engine.exe.
