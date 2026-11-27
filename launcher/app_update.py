"""Apply a verified EXE after the Windows tray process has stopped."""
import ctypes
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from ctypes import wintypes
from pathlib import Path

import httpx

from backend.app_updates import UpdateError, download_release, latest_release, version_tuple

STAGING_PREFIX = ".prospero-update-"


class UpdateRolledBack(UpdateError):
    pass


def start_exe(executable, *args):
    return subprocess.Popen([str(executable), *map(str, args)], cwd=executable.parent,
                            env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"})


class TrayUpdater:
    def __init__(self, target, database_path, port, shutdown):
        self.target = target
        self.database_path = database_path
        self.port = port
        self.shutdown = shutdown

    def prepare(self, current, progress):
        # GitHub serves the same public asset to the browser and the app.
        with httpx.Client(timeout=httpx.Timeout(30, connect=8), follow_redirects=False) as client:
            release = latest_release(client, current)
            if release is None:
                return None
            staged = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=self.target.parent))
            try:
                progress(release.version, 0, release.size)
                download_release(client, release, staged / "pending.exe",
                                 lambda received: progress(release.version, received, release.size))
            except Exception:
                shutil.rmtree(staged, ignore_errors=True)
                raise
        return release.version, staged

    def launch(self, prepared):
        version, staged = prepared
        helper = staged / "helper.exe"
        shutil.copyfile(self.target, helper)
        start_exe(helper, "--apply-update", os.getpid(), self.port, version, self.target, self.database_path)


def wait_for_exit(pid):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel.OpenProcess
    open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_process.restype = wintypes.HANDLE
    wait = kernel.WaitForSingleObject
    wait.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    wait.restype = wintypes.DWORD
    close = kernel.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    close.restype = wintypes.BOOL
    handle = open_process(0x00100000, False, pid)
    if not handle:
        if ctypes.get_last_error() == 87:
            return
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if wait(handle, 90_000) != 0:
            raise UpdateError("Предыдущий экземпляр Prospero ещё работает.")
    finally:
        close(handle)


def replace_exe(source, target):
    # A one-file PyInstaller bootloader can hold the EXE shortly after Python exits.
    deadline = time.monotonic() + 15
    while True:
        try:
            source.replace(target)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(.25)


def copy_database(source, target):
    with closing(sqlite3.connect(source)) as original, closing(sqlite3.connect(target)) as copy:
        original.backup(copy)


def stop_child(process):
    if process.poll() is not None:
        return
    taskkill = Path(os.environ["SystemRoot"], "System32", "taskkill.exe")
    subprocess.run([str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   creationflags=subprocess.CREATE_NO_WINDOW, timeout=12)
    process.wait(timeout=10)


def activate_update(staged, target, database_path, version, ready):
    old_exe = staged / "previous.exe"
    old_database = staged / "library-before-update.db"
    shutil.copyfile(target, old_exe)
    if database_path.is_file():
        copy_database(database_path, old_database)
    replace_exe(staged / "pending.exe", target)
    child = None
    try:
        child = start_exe(target, "--background")
        if not ready(child, version):
            raise UpdateError("Новая версия Prospero не запустилась.")
    except Exception:
        if child is not None:
            stop_child(child)
        if old_database.is_file():
            copy_database(old_database, database_path)
        replace_exe(old_exe, target)
        start_exe(target, "--background")
        raise UpdateRolledBack("Новая версия не запустилась. Предыдущая версия восстановлена.") from None


def apply_update(pid, port, version, target, database_path, wait_health):
    staged = Path(sys.executable).resolve().parent
    target = Path(target).resolve()
    database_path = Path(database_path).resolve()
    port = int(port)
    version_tuple(version)
    if staged.parent != target.parent or not staged.name.startswith(STAGING_PREFIX) or not 1 <= port <= 65535:
        raise UpdateError("Некорректное расположение файла обновления Prospero.")
    wait_for_exit(int(pid))
    try:
        activate_update(staged, target, database_path, version, lambda process, expected: wait_health(
            f"http://127.0.0.1:{port}", process=process, version=expected, timeout=35,
        ))
    except UpdateRolledBack:
        (staged / "finished").touch()
        raise
    except (OSError, sqlite3.Error) as error:
        if (staged / "pending.exe").exists() and target.is_file():
            start_exe(target, "--background")
            (staged / "finished").touch()
        raise UpdateError(f"Не удалось завершить обновление Prospero: {error}") from error
    else:
        # Windows holds helper.exe open until this process exits.
        (staged / "finished").touch()


def clean_finished_updates(directory):
    for staged in directory.glob(f"{STAGING_PREFIX}*"):
        if (staged / "finished").is_file():
            shutil.rmtree(staged, ignore_errors=True)
