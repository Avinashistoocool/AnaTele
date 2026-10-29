# PyInstaller spec for the AnaTele desktop app: pyinstaller anatele.spec
# Produces dist/AnaTele/ (one-folder build). The same executable runs analysis workers
# via the hidden "--worker-job" argument, so no separate Python install is needed.
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

datas = [(f"telemetry_platform/{folder}", f"telemetry_platform/{folder}") for folder in ("static", "rules", "semantics")]
datas += collect_data_files("pymavlink", includes=["dialects/v20/*.xml", "dialects/v10/*.xml"])
hidden = collect_submodules("pymavlink.dialects") + collect_submodules("uvicorn") + [
    "telemetry_platform.analysis", "telemetry_platform.worker", "telemetry_platform.server",
    "mpl_toolkits.mplot3d", "matplotlib.backends.backend_qtagg",
]

a = Analysis(["anatele.py"], datas=datas, hiddenimports=hidden,
             excludes=["tkinter", "PySide6.QtWebEngineCore", "PySide6.Qt3DCore", "PySide6.QtQuick"])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="AnaTele", console=False,
          icon="telemetry_platform/static/icon.png")
coll = COLLECT(exe, a.binaries, a.datas, name="AnaTele")
