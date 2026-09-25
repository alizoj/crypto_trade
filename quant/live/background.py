"""Фоновый `quant live run`: `quant live start | stop | status`.

Состояние — data/live.pid (JSON: pid, адрес, интерпретатор), вывод процесса — data/logs/live.log
(предыдущий запуск — live.log.1). Остановка жёсткая, как Ctrl+C: live-процесс пишет только в SQLite
транзакциями, сбрасывать на диск при выходе нечего.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from quant.config import get_settings

START_TIMEOUT = 90.0
STOP_TIMEOUT = 10.0


def _pid_file() -> Path:
    return get_settings().data_dir / "live.pid"


def _log_file() -> Path:
    return get_settings().data_dir / "logs" / "live.log"


def _read_state() -> dict | None:
    try:
        return json.loads(_pid_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _image(pid: int) -> str | None:
    """Путь к исполняемому файлу живого процесса; None — процесса нет, "" — жив, но путь не узнать."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                   ctypes.POINTER(wintypes.DWORD)]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return None
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != 259:  # STILL_ACTIVE
                return None
            buf = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buf))
            return buf.value if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)) else ""
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return ""


def _running(state: dict | None) -> bool:
    """Жив ли процесс из pid-файла. Сверяем интерпретатор: после перезагрузки PID мог достаться чужому процессу."""
    if not state:
        return False
    img = _image(int(state["pid"]))
    return img is not None and (img == "" or _same_path(img, state["exe"]))


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1" if host in ("0.0.0.0", "") else host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _url(state: dict) -> str:
    host = "127.0.0.1" if state["host"] in ("0.0.0.0", "") else state["host"]
    return f"http://{host}:{state['port']}"


def _spawn(argv: list[str], log: Path) -> subprocess.Popen:
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    with open(log, "ab") as out:
        kw = dict(stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, env=env)
        if sys.platform != "win32":
            return subprocess.Popen(argv, start_new_session=True, **kw)
        # Скрытая собственная консоль: процесс переживает закрытие терминала и не ловит его Ctrl+C
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            # выйти из job терминала, иначе он может убить процесс при закрытии
            return subprocess.Popen(argv, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **kw)
        except OSError:  # job не разрешает выход — запускаем внутри
            return subprocess.Popen(argv, creationflags=flags, **kw)


def _kill(pid: int, force: bool) -> None:
    if sys.platform == "win32":
        # /T — вместе с потомками: python.exe из venv на Windows — лаунчер, интерпретатор — его дочерний процесс
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        try:
            os.killpg(pid, signal.SIGKILL if force else signal.SIGTERM)  # start_new_session → pgid == pid
        except ProcessLookupError:
            pass


def _tail(path: Path, n: int = 30) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:])
    except OSError:
        return ""


def start(argv: list[str], host: str, port: int) -> int:
    state = _read_state()
    if _running(state):
        print(f"Уже запущено: {_url(state)} (PID {state['pid']}). Остановить: quant live stop")
        return 1
    if _port_open(host, port):
        print(f"Порт {port} уже занят другим процессом — укажите другой: --port 8001")
        return 1

    log = _log_file()
    log.parent.mkdir(parents=True, exist_ok=True)
    try:
        log.replace(log.with_name(log.name + ".1"))
    except OSError:  # лога ещё нет или он занят — тогда дописываем в него
        pass
    proc = _spawn(argv, log)
    state = {"pid": proc.pid, "host": host, "port": port, "exe": argv[0], "log": str(log),
             "started": datetime.now().isoformat(timespec="seconds")}
    _pid_file().write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            _pid_file().unlink(missing_ok=True)
            print(f"Процесс завершился при запуске (код {proc.returncode}). Последние строки {log}:\n{_tail(log)}")
            return 1
        if _port_open(host, port):
            print(f"Запущено в фоне: {_url(state)} (PID {proc.pid})\nЛог: {log}\nОстановить: quant live stop")
            return 0
        time.sleep(0.5)
    print(f"Процесс работает (PID {proc.pid}), но порт {port} за {START_TIMEOUT:.0f} с так и не открылся — "
          f"смотрите лог: {log}")
    return 1


def stop() -> int:
    state = _read_state()
    if not _running(state):
        _pid_file().unlink(missing_ok=True)
        print("Не запущено.")
        return 0
    pid = int(state["pid"])
    _kill(pid, force=False)
    deadline = time.monotonic() + STOP_TIMEOUT
    while _running(state):
        if time.monotonic() > deadline:
            _kill(pid, force=True)
            time.sleep(0.5)
            if _running(state):
                print(f"Не удалось остановить PID {pid}.")
                return 1
            break
        time.sleep(0.2)
    _pid_file().unlink(missing_ok=True)
    print(f"Остановлено (PID {pid}).")
    return 0


def status() -> int:
    state = _read_state()
    if not _running(state):
        print("Не запущено.")
        return 1
    up = "отвечает" if _port_open(state["host"], int(state["port"])) else "порт ещё не открыт"
    print(f"Работает: {_url(state)} ({up}), PID {state['pid']}, с {state['started']}\nЛог: {state['log']}")
    return 0
