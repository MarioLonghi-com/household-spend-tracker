# The desktop app's backend, frozen with PyInstaller --onedir (#319).
#
#   pyinstaller desktop/backend.spec --noconfirm --distpath build/backend --workpath build/pyinstaller
#
# One folder, never one file: a one-file build unpacks itself into a temporary
# directory and re-executes, which the Mac App Store sandbox refuses.
#
# What the analysis cannot see, and so is named here -- each one found missing
# from a build that otherwise started (spike #59):
# - every file under app/ and statements/ that is not Python: the built
#   client, /snap's page, the common-password list, the seed catalogue;
#   PyInstaller's collect_data_files found none of them;
# - the migrations, which Alembic loads from files at run time, and every
#   module they import;
# - the native packages that carry data or libraries of their own.
import ast
import os
import pathlib

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))  # noqa: F821 - PyInstaller defines it

hidden = collect_submodules("app") + collect_submodules("statements")
hidden += ["scripts", "scripts.backup", "scripts.upgrade"]
datas = [(os.path.join(ROOT, "migrations"), "migrations")]

for source in pathlib.Path(ROOT, "migrations").rglob("*.py"):
    for node in ast.walk(ast.parse(source.read_text())):
        if isinstance(node, ast.Import):
            hidden += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            hidden.append(node.module)

for package in ("app", "statements"):
    for path in pathlib.Path(ROOT, package).rglob("*"):
        if path.is_file() and path.suffix not in {".py", ".pyc"} and "__pycache__" not in path.parts:
            datas.append((str(path), str(path.parent.relative_to(ROOT))))

binaries = []
for package in ("pypdfium2", "pypdfium2_raw", "pdfminer", "pdfplumber", "pillow_heif", "certifi", "webauthn"):
    found_datas, found_binaries, found_hidden = collect_all(package)
    datas += found_datas
    binaries += found_binaries
    hidden += found_hidden

# What the frozen backend reports it is made of (#453). PyInstaller copies a
# package's code but rarely its dist-info, so importlib.metadata inside the
# freeze finds a dozen of the ~45 packages it carries. The list is written here,
# at freeze time, from the same lock-less-excludes set the notices use, and
# bundled as app/frozen-packages.json for app.services.platform.packages().
import json
import sys

sys.path.insert(0, ROOT)
from scripts.third_party_notices import frozen_packages  # noqa: E402

shipped = pathlib.Path(ROOT, "build", "frozen-packages.json")
shipped.parent.mkdir(exist_ok=True)
shipped.write_text(
    json.dumps(
        frozen_packages(
            pathlib.Path(ROOT, "requirements.txt").read_text(encoding="utf-8"),
            pathlib.Path(SPECPATH, "backend.spec").read_text(encoding="utf-8"),  # noqa: F821
        ),
        indent=1,
    ),
    encoding="utf-8",
)
datas.append((str(shipped), "app"))

a = Analysis(  # noqa: F821
    [os.path.join(SPECPATH, "backend_entry.py")],  # noqa: F821
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    # Not under stdio: uvicorn and its extras serve sockets.
    excludes=["uvicorn", "uvloop", "watchfiles", "httptools", "websockets", "tkinter", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821
# UTF-8 mode baked into the build: the frozen interpreter ignores PYTHON*
# variables, so the shell's PYTHONUTF8=1 never reached it (#319), and Windows
# pipes would otherwise carry the ANSI code page.
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [("X utf8", None, "OPTION")],
    exclude_binaries=True,
    name="spend-tracker-backend",
    console=True,
)
coll = COLLECT(exe, a.binaries, a.datas, name="spend-tracker-backend")  # noqa: F821
