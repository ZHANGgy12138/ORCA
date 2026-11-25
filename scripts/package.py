"""Make a portable Windows release with the exact corresponding source."""
import hashlib
from pathlib import Path
import shutil
import zipfile

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "dist" / "ORCA-0.9.1"
RELEASE = ROOT / "release"


def main():
    if not (APP / "ORCA.exe").is_file():
        raise RuntimeError("Build the desktop executable first.")
    RELEASE.mkdir(exist_ok=True)
    source = RELEASE / "ORCA-0.9.1-source.zip"
    names = ["app.py", "resume_store.py", "image_translation.py", "startup_service.py", "single_instance.py", "window_chrome.py", "desktop_api.py", "reader_service.py", "reader_bing.py", "engine_registry.py", "credential_store.py", "engine_diagnostics.py", "preferences.py", "desktop_compat.py", "runtime_hook.py", "desktop.spec", "build.ps1", "version_info.txt", "app.ico", "README.md", "RELEASE_NOTES.md", "LICENSE", "requirements-build.txt", "vendor-changes.patch", "ui", "vendor", "scripts", "licenses"]
    with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name in names:
            path = ROOT / name
            paths = path.rglob("*") if path.is_dir() else [path]
            for item in paths:
                if item.is_file() and "__pycache__" not in item.parts and item.suffix != ".pyc":
                    archive.write(item, item.relative_to(ROOT).as_posix())
    for name in ("README.md", "RELEASE_NOTES.md", "LICENSE"):
        shutil.copy2(ROOT / name, APP / name)
    shutil.copytree(ROOT / "licenses", APP / "licenses", dirs_exist_ok=True)
    shutil.copy2(source, APP / "SOURCE.zip")
    portable = RELEASE / "ORCA-0.9.1-windows-x64.zip"
    with zipfile.ZipFile(portable, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for item in APP.rglob("*"):
            if item.is_file():
                archive.write(item, (Path("ORCA") / item.relative_to(APP)).as_posix())
    lines = []
    for file in (portable, source):
        digest = hashlib.file_digest(file.open("rb"), "sha256").hexdigest()
        lines.append(f"{digest}  {file.name}")
        print(file.name, f"{file.stat().st_size / 1048576:.1f} MB", digest)
    (RELEASE / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="ascii")


if __name__ == "__main__":
    main()
