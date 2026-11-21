import os
from pathlib import Path
import re
import subprocess
import winreg

NAME = "AnastasiyaAI"
HOST = Path(__file__).with_name("AnastasiyaAI.exe").resolve()
SC = Path(os.environ["SystemRoot"]) / "System32" / "sc.exe"


def command(*args, allowed=(0,)):
    result = subprocess.run([SC, *args], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode not in allowed:
        raise OSError(f"Windows вернула код {result.returncode} при операции {args[0]}.")
    return result


def status():
    result = command("query", NAME, allowed=(0, 1060))
    if result.returncode == 1060:
        return 0
    # sc prints a numeric service state followed by its symbolic name in every locale.
    match = re.search(rb"(?m)^\s*[^:\r\n]+:\s*([1-7])\s+\S", result.stdout)
    if not match:
        raise OSError("Windows вернула неизвестное состояние службы.")
    return int(match[1])


def check_owner():
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, "SYSTEM\\CurrentControlSet\\Services\\" + NAME) as key:
        image = winreg.QueryValueEx(key, "ImagePath")[0]
        account = winreg.QueryValueEx(key, "ObjectName")[0]
    if not image.endswith('" --service') or not image.startswith('"'):
        raise ValueError("Имя AnastasiyaAI уже занято другой службой.")
    binary = Path(image[1:-len('" --service')]).resolve()
    if binary.parent.parent != HOST.parent.parent or binary.name != HOST.name or account.lower() != ("NT SERVICE\\" + NAME).lower():
        raise ValueError("Имя AnastasiyaAI уже занято другой службой.")


def install_start():
    state = status()
    args = ["binPath=", f'"{HOST}" --service', "start=", "delayed-auto", "obj=", "NT SERVICE\\" + NAME]
    if not state:
        command("create", NAME, *args, "DisplayName=", "Анастасия AI")
    else:
        check_owner()
        command("config", NAME, *args)
    command("sidtype", NAME, "unrestricted")
    command("start", NAME, allowed=(0, 1056))


def stop():
    check_owner()
    command("stop", NAME, allowed=(0, 1062))