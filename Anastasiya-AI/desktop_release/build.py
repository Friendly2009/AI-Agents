from pathlib import Path
import os
import subprocess
import sys
import sysconfig
import zipfile
import importlib.metadata as metadata
from packaging.requirements import Requirement

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
PYTHON = Path(sys.base_prefix)
PAYLOAD = HERE / "agent.zip"
OUTPUT = HERE.parent / "outputs" / "Anastasiya-AI-Windows-x64.exe"
PROFILE_MODULES = {"app_settings", "autojoin", "campaigns", "city_sources", "cli",
                   "community_inspector", "director_orders", "family_radar", "admin_panel"}


def runtime_deps():
    pending = ["telethon", "openai", "python-dotenv", "qrcode"]
    seen = set()
    while pending:
        dist = metadata.distribution(pending.pop())
        if dist.name in seen:
            continue
        seen.add(dist.name)
        yield dist
        for line in dist.requires or []:
            req = Requirement(line)
            if req.marker is None or req.marker.evaluate():
                pending.append(req.name)


def build():
    with zipfile.ZipFile(PAYLOAD, "w", zipfile.ZIP_DEFLATED, compresslevel=5) as z:
        for file in ("agent.ico", "desktop.py", "profile_store.py", "windows_service.py"):
            z.write(HERE / file, file)
        z.write(PROJECT / ".env.example", "defaults.env")
        for file in ("resources/knowledge.json", "resources/sales_expertise.md", "config/approved_groups.json"):
            z.write(PROJECT / file, file)
        for file in (PROJECT / "src").glob("*.py"):
            source = file.read_text(encoding="utf-8-sig")
            if file.stem in PROFILE_MODULES:
                source = source.replace("Path(__file__).resolve().parents[1]", "PROFILE")
                source = source.replace("from pathlib import Path", "from pathlib import Path\nfrom profile_store import PROFILE")
            # Explicit database arguments cover the main application. Defaults also
            # occur in command handlers which open their own resources.
            source = source.replace("Path(__file__).resolve().parents[1] / 'data'", "PROFILE / 'data'")
            source = source.replace('Path(__file__).resolve().parents[1] / "data"', 'PROFILE / "data"')
            if "PROFILE / " in source and "from profile_store import PROFILE" not in source:
                source = source.replace("from pathlib import Path", "from pathlib import Path\nfrom profile_store import PROFILE", 1)
            z.writestr("src/" + file.name, source.encode("utf-8"))
        for name in ("python.exe", "pythonw.exe", "python3.dll", "python314.dll", "vcruntime140.dll",
                     "vcruntime140_1.dll", "LICENSE.txt"):
            z.write(PYTHON / name, "runtime/" + name)
        for folder in ("DLLs", "tcl", "Lib"):
            for root, dirs, names in os.walk(PYTHON / folder):
                dirs[:] = [d for d in dirs if d not in {"__pycache__", "site-packages", "test", "tests", "idlelib", "ensurepip"}]
                for name in names:
                    file = Path(root) / name
                    z.write(file, "runtime/" + file.relative_to(PYTHON).as_posix())
        z.writestr("runtime/python314._pth", ".\nDLLs\nLib\nLib/site-packages\n")
        site = Path(sysconfig.get_path("purelib"))
        copied = set()
        for dist in runtime_deps():
            for name in dist.files:
                file = Path(dist.locate_file(name)).resolve()
                if not file.is_relative_to(site) or file.suffix == ".pyc" or file in copied or not file.is_file():
                    continue
                copied.add(file)
                z.write(file, "runtime/Lib/site-packages/" + file.relative_to(site).as_posix())
    OUTPUT.parent.mkdir(exist_ok=True)
    compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    subprocess.run([compiler, "/nologo", "/target:winexe", "/platform:x64", "/win32icon:" + str(HERE / "agent.ico"), "/codepage:65001",
                    "/reference:System.IO.Compression.dll", "/reference:System.IO.Compression.FileSystem.dll", "/reference:System.ServiceProcess.dll",
                    "/resource:" + str(PAYLOAD) + ",agent.zip", "/out:" + str(OUTPUT), HERE / "Launcher.cs"], check=True)
    print(OUTPUT, OUTPUT.stat().st_size)


if __name__ == "__main__":
    build()
