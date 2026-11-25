"""Windows tray entry point, packaged by scripts/build-windows.ps1."""
import ctypes
import json
import os
import re
import socket
import sys
import time
import webbrowser
from ctypes import wintypes
from pathlib import Path
from threading import Thread
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener

import uvicorn
from dotenv import load_dotenv
from PIL import Image, ImageDraw

from backend.database import BASE, DEFAULT_DATA

TITLE = "Prospero Library"
REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
REG_VALUE = "ProsperoLibrary"
LOCAL_HTTP = build_opener(ProxyHandler({}))


def data_directory():
    portable = Path(sys.executable).parent if getattr(sys, "frozen", False) else BASE
    load_dotenv(portable / ".env")
    load_dotenv(DEFAULT_DATA / ".env")
    return Path(os.getenv("PROSPERO_DATA") or DEFAULT_DATA).expanduser().resolve()


def tray_icon():
    image = Image.new("RGBA", (64, 64))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((1, 1, 62, 62), radius=14, fill="#e7b772")
    draw.rounded_rectangle((14, 22, 49, 48), radius=4, outline="#1d1915", width=4)
    draw.rounded_rectangle((12, 15, 51, 25), radius=3, outline="#1d1915", width=4)
    draw.line((27, 33, 37, 33), fill="#1d1915", width=4)
    return image


def notify_error(message):
    ctypes.windll.user32.MessageBoxW(None, str(message), TITLE, 0x10)


def wait_health(base, thread=None):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if thread is not None and not thread.is_alive():
            return False
        try:
            with LOCAL_HTTP.open(base + "/api/health", timeout=.5) as response:
                return json.load(response).get("app") == "prospero-library"
        except (OSError, ValueError):
            time.sleep(.17)
    return False


def autostart():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY) as key:
            return bool(winreg.QueryValueEx(key, REG_VALUE)[0])
    except FileNotFoundError:
        return False


def toggle_start(icon, _):
    import winreg
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REG_KEY) as key:
            if autostart():
                winreg.DeleteValue(key, REG_VALUE)
            else:
                winreg.SetValueEx(key, REG_VALUE, 0, winreg.REG_SZ, '"' + sys.executable + '" --background')
    except OSError:
        notify_error("Не удалось изменить автозапуск")
    icon.update_menu()


def mutex():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateMutexW
    create.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    create.restype = wintypes.HANDLE
    user = re.sub(r"\W", "_", os.environ.get("USERNAME", "User"))
    handle = create(None, False, "Local\\ProsperoLibrary-" + user)
    error = ctypes.get_last_error()
    if not handle:
        raise ctypes.WinError(error)
    close = kernel.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    close.restype = wintypes.BOOL
    return handle, error == 183, close


def tray_menu(pystray, server, base, directory):
    def open_page(path=""):
        return lambda *_: webbrowser.open(base + path)

    def open_logs(*_):
        os.startfile(str(directory / "logs"))

    def start_job(path, payload=None):
        def launch(*_):
            def request():
                try:
                    query = Request(base + "/api/" + path, data=payload, method="POST",
                                    headers={"Content-Type": "application/json"})
                    with LOCAL_HTTP.open(query, timeout=3):
                        pass
                except URLError as error:
                    if getattr(error, "code", None) != 409:
                        notify_error("Не удалось запустить операцию")
                        return
                webbrowser.open(base + "/?job=1")
            Thread(target=request, daemon=True).start()
        return launch

    def quit_tray(icon, _):
        server.should_exit = True
        icon.stop()

    item = pystray.MenuItem
    return pystray.Menu(
        item("Открыть библиотеку", open_page(), default=True),
        item("Сканировать коллекцию", start_job("scan", b'{"force":false}')),
        item("Проверить обновления", start_job("updates")),
        item("Настройки", open_page("/settings")),
        item("Журнал работы", open_logs),
        item("Запускать вместе с Windows", toggle_start, checked=lambda _: autostart(),
             enabled=getattr(sys, "frozen", False)),
        item("Выход", quit_tray),
    )


def run_tray():
    import pystray
    directory = data_directory()
    port = int(os.getenv("PROSPERO_PORT", "8000"))
    if not 1 <= port <= 65535:
        raise ValueError("PROSPERO_PORT должен быть от 1 до 65535")
    base = f"http://127.0.0.1:{port}"
    handle, already_running, close = mutex()
    try:
        if already_running:
            if wait_health(base):
                webbrowser.open(base)
            else:
                notify_error("Запущенный экземпляр не отвечает по настроенному адресу")
            return

        from backend.main import create_app
        # Pass the same listening socket to Uvicorn so the port stays reserved.
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                listener.bind(("127.0.0.1", port))
                listener.listen(64)
            except OSError as error:
                raise RuntimeError(f"Порт {port} занят. Измените PROSPERO_PORT в .env.") from error
            app = create_app(directory)
            server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                log_config=None, access_log=False, proxy_headers=False, timeout_graceful_shutdown=15))
            runner = Thread(target=server.run, kwargs={"sockets": [listener]}, name="prospero-http", daemon=True)
            runner.start()
            try:
                if not wait_health(base, runner):
                    raise RuntimeError("Локальный сервер не запустился. Проверьте каталог данных и журнал.")
                if "--background" not in sys.argv:
                    webbrowser.open(base)
                pystray.Icon("prospero-library", tray_icon(), TITLE, tray_menu(pystray, server, base, directory)).run()
            finally:
                server.should_exit = True
                runner.join(timeout=65)
    finally:
        close(handle)


if __name__ == "__main__":
    if os.name != "nt":
        raise SystemExit("Запускатор трея предназначен для Windows")
    try:
        run_tray()
    except Exception as error:
        notify_error(error)
        raise SystemExit(1)
