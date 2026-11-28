import os
from pathlib import Path
import re
import subprocess
import time
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
    return binary


def wait_state(expected, transitional):
    deadline = time.monotonic() + 35
    while True:
        state = status()
        if state == expected:
            return
        if state not in transitional or time.monotonic() >= deadline:
            raise OSError(f"Служба в состоянии {state}, ожидалось {expected}.")
        time.sleep(0.3)


def upgrade_existing():
    state = status()
    if not state:
        return
    previous = check_owner()
    if previous == HOST:
        return
    if state == 2:
        wait_state(4, {2})
        state = 4
    if state == 3:
        wait_state(1, {3})
        state = 1
    if state not in {1, 4}:
        raise OSError("Дождитесь остановки или запуска службы и откройте приложение снова.")
    running = state == 4
    if running:
        command("stop", NAME)
        wait_state(1, {3, 4})
    try:
        command("config", NAME, "binPath=", f'"{HOST}" --service')
        if check_owner() != HOST:
            raise OSError("Windows не подтвердила путь к новой версии.")
        if running:
            command("start", NAME)
            wait_state(4, {2})
    except (ValueError, OSError):
        if status() == 1:
            command("config", NAME, "binPath=", f'"{previous}" --service')
            if running:
                command("start", NAME)
        raise



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