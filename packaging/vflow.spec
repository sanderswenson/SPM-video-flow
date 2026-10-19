# PyInstaller spec for the Windows app. Run from the repo root:
#   pyinstaller packaging/vflow.spec --noconfirm
# Expects ffmpeg.exe + ffprobe.exe in packaging/ffmpeg/ (CI downloads them).
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH).parent

datas = [
    (str(ROOT / "vflow" / "app" / "static"), "vflow/app/static"),
    (str(ROOT / "vflow" / "fonts"), "vflow/fonts"),
    (str(ROOT / "packaging" / "ffmpeg"), "ffmpeg"),
]
binaries = []
hiddenimports = ["keyring.backends.Windows", "vflow._build"]
for pkg in ("faster_whisper", "ctranslate2", "tokenizers", "webview"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    [str(ROOT / "packaging" / "launch.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "torch"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="vflow",
    console=False,
    icon=str(ROOT / "packaging" / "vflow.ico"),
)
coll = COLLECT(exe, a.binaries, a.datas, name="vflow")
