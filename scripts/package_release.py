"""Package the committed source and the built frontend for GitHub Releases."""
import argparse
import re
import runpy
import subprocess
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "release")
    output = parser.parse_args().output_dir.resolve()
    version = runpy.run_path(str(ROOT / "backend" / "__init__.py"))["__version__"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        parser.error("Use a release version in the form X.Y.Z")

    frontend = ROOT / "frontend" / "dist"
    if not (frontend / "index.html").is_file():
        parser.error("Build the frontend with npm --prefix frontend run build")
    output.mkdir(parents=True, exist_ok=True)
    artifact = output / f"Prospero-Library-{version}.zip"
    subprocess.run([
        "git", "archive", "--format=zip", "--prefix=Prospero-Library/", f"--output={artifact}", "HEAD",
    ], cwd=ROOT, check=True)
    with ZipFile(artifact, "a", compression=ZIP_DEFLATED) as archive:
        for file in sorted(frontend.rglob("*")):
            if file.is_file():
                archive.write(file, "Prospero-Library/" + file.relative_to(ROOT).as_posix())
        if bad_entry := archive.testzip():
            raise RuntimeError(f"Archive verification failed for {bad_entry}")

    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    section = re.search(rf"^## {re.escape(version)}(?:[ \t]+[^\n]*)?\n(.*?)(?=^## |\Z)", changelog, re.M | re.S)
    notes = (
        "Для Windows x64 скачай EXE из Assets и запусти его. "
        "EXE должен лежать рядом с прежней папкой `data`, если Prospero уже установлена.\n"
    )
    if section and section[1].strip():
        notes += "\n" + section[1].strip() + "\n"
    (output / "release-notes.md").write_text(notes, encoding="utf-8")
    print(artifact)


if __name__ == "__main__":
    main()
