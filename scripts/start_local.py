"""Prepare the local Python environment and start the catalog."""
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
VENV = ROOT / ".venv"
PYTHON = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
REQUIREMENTS = ROOT / "requirements.txt"
IMPORT_CHECK = (
    "import fastapi, uvicorn, httpx, truststore, python_multipart; "
    "from PIL import Image; from dotenv import load_dotenv"
)


def run(executable, *arguments, quiet=False, check=False):
    output = subprocess.DEVNULL if quiet else None
    return subprocess.run(
        [str(executable), *arguments], cwd=ROOT, stdout=output, stderr=output, check=check,
    ).returncode


def prepare():
    if sys.version_info < (3, 12):
        raise RuntimeError("Prospero Library requires Python 3.12 or newer.")
    if not (ROOT / "frontend/dist/index.html").is_file() or not REQUIREMENTS.is_file():
        raise RuntimeError("Extract the entire Prospero Library ZIP before starting.")

    usable = PYTHON.is_file() and run(
        PYTHON, "-c", "import sys; sys.exit(sys.version_info < (3, 12))", quiet=True,
    ) == 0
    if not usable:
        print("Preparing the local Python environment...", flush=True)
        run(sys.executable, "-m", "venv", str(VENV), check=True)

    if run(PYTHON, "-c", IMPORT_CHECK, quiet=True):
        print("Installing Prospero Library dependencies...", flush=True)
        if run(PYTHON, "-m", "pip", "--version", quiet=True):
            run(PYTHON, "-m", "ensurepip", "--upgrade", check=True)
        run(PYTHON, "-m", "pip", "--disable-pip-version-check", "install", "-r", str(REQUIREMENTS), check=True)
        run(PYTHON, "-c", IMPORT_CHECK, check=True)


def main():
    try:
        prepare()
        return run(PYTHON, "-m", "backend")
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Prospero Library could not start: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
