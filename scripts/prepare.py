"""Collect reproducible engine sources and redistributable resources."""
import argparse
import difflib
import importlib.metadata
import json
from pathlib import Path
import re
import shutil
import sys

ROOT = Path(__file__).resolve().parent.parent


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets", required=True, type=Path)
    args = parser.parse_args()
    site = Path(sys.prefix) / "Lib" / "site-packages"
    changes = []
    for name in ("pdf2zh_next", "babeldoc"):
        # Distributed vendor sources contain our tested changes; never overwrite them.
        if not (ROOT / "vendor" / name).exists():
            shutil.copytree(site / name, ROOT / "vendor" / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    high_level = ROOT / "vendor" / "pdf2zh_next" / "high_level.py"
    source = high_level.read_text(encoding="utf-8")
    if "translation_finished = False" not in source:
        changed = source.replace("    cancel_flag = False\n", "    cancel_flag = False\n    translation_finished = False\n", 1)
        changed = changed.replace("            yield event.args[0]\n", '            if event.args[0].get("type") == "finish":\n                translation_finished = True\n            yield event.args[0]\n', 1)
        changed = changed.replace("        if not cancel_flag:\n", "        if not cancel_flag and not translation_finished:\n", 1)
        changes.extend(difflib.unified_diff(source.splitlines(True), changed.splitlines(True), fromfile="pdf2zh_next/high_level.py", tofile="pdf2zh_next/high_level.py"))
        write(high_level, changed)
    for module, old, new in [
        ("babeldoc/const.py", 'CACHE_FOLDER = Path.home() / ".cache" / "babeldoc"', 'CACHE_FOLDER = Path(os.environ["PDFMT_CACHE_DIR"]) if os.environ.get("PDFMT_CACHE_DIR") else Path.home() / ".cache" / "babeldoc"'),
        ("pdf2zh_next/const.py", 'from pathlib import Path', 'from pathlib import Path\nimport os'),
        ("pdf2zh_next/const.py", 'DEFAULT_CONFIG_DIR = Path("~/.config/pdf2zh").expanduser()', 'DEFAULT_CONFIG_DIR = Path(os.environ["PDFMT_DATA_DIR"]) / "config" if os.environ.get("PDFMT_DATA_DIR") else Path("~/.config/pdf2zh").expanduser()'),
        ("pdf2zh_next/translator/cache.py", 'import logging', 'import logging\nimport os'),
        ("pdf2zh_next/translator/cache.py", '    cache_folder = Path.home() / ".cache" / "pdf2zh_next"', '    cache_folder = (Path(os.environ["PDFMT_DATA_DIR"]) / "cache" / "translation"\n                    if os.environ.get("PDFMT_DATA_DIR")\n                    else Path.home() / ".cache" / "pdf2zh_next")'),
        ("pdf2zh_next/high_level.py", '    logger = logging.getLogger(__name__)\n    cancel_event = threading.Event()', '    from desktop_compat import install\n    install()\n    logger = logging.getLogger(__name__)\n    cancel_event = threading.Event()'),
    ]:
        path = ROOT / "vendor" / module
        source = path.read_text(encoding="utf-8")
        if new in source:
            continue
        if old not in source:
            raise RuntimeError(f"Upstream source changed: {module}: {old[:40]}")
        changed = source.replace(old, new, 1)
        changes.extend(difflib.unified_diff(source.splitlines(True), changed.splitlines(True), fromfile=module, tofile=module))
        write(path, changed)
    if changes:
        write(ROOT / "vendor-changes.patch", "".join(changes))
    origin = ROOT.parent / "PDFMathTranslate-local" / "free_engine_compat.py"
    if not (ROOT / "desktop_compat.py").is_file():
        if not origin.is_file():
            raise RuntimeError("desktop_compat.py must be present in the source package.")
        shutil.copy2(origin, ROOT / "desktop_compat.py")
    for folder in ("fonts", "models", "cmap", "tiktoken"):
        if not (args.assets / folder).is_dir():
            raise RuntimeError(f"Missing asset folder: {folder}")
        shutil.copytree(args.assets / folder, ROOT / "resources" / "babeldoc" / folder, dirs_exist_ok=True)
    licenses = ROOT / "licenses"
    licenses.mkdir(exist_ok=True)
    inventory = []
    for dist in importlib.metadata.distributions(path=[str(site)]):
        name = dist.metadata.get("Name", "unknown")
        if name.lower() in ("pyinstaller", "pyinstaller-hooks-contrib", "setuptools", "altgraph", "pefile", "pywin32-ctypes"):
            continue
        inventory.append({"name": name, "version": dist.version, "license": dist.metadata.get("License-Expression") or dist.metadata.get("License", "See upstream package license")})
        for item in dist.files or []:
            if Path(str(item)).name.lower().startswith(("license", "copying", "notice")) and ".dist-info/" in str(item).replace("\\", "/"):
                source = Path(dist.locate_file(item))
                if source.is_file():
                    dest = licenses / re.sub(r"[^A-Za-z0-9_.-]", "_", name) / Path(str(item)).name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, dest)
    write(licenses / "DEPENDENCIES.json", json.dumps(sorted(inventory, key=lambda d: d["name"].lower()), ensure_ascii=False, indent=2))
    from fontTools.ttLib import TTFont
    font_notices = []
    for path in sorted((args.assets / "fonts").glob("*.ttf")):
        with TTFont(path, lazy=True) as font:
            notices = sorted({record.toUnicode() for record in font["name"].names if record.nameID in (0, 13, 14)})
        font_notices.append(path.name + "\n" + "\n".join(notices) + "\n")
    write(licenses / "FONT_NOTICES.txt", "\n".join(font_notices))
    engine_license = next((licenses / "pdf2zh-next").glob("LICENSE*"), None)
    if not engine_license:
        engine_license = next((site / "pdf2zh_next").glob("LICENSE*"), None)
    if engine_license:
        shutil.copy2(engine_license, ROOT / "LICENSE")
    else:
        import requests
        import truststore
        truststore.inject_into_ssl()
        response = requests.get("https://raw.githubusercontent.com/PDFMathTranslate-next/PDFMathTranslate-next/main/LICENSE", timeout=30)
        response.raise_for_status()
        write(ROOT / "LICENSE", response.text)
    from make_icon import make_icon
    make_icon(ROOT / "app.ico")
    print("Prepared engine sources, public assets, licenses and app icon.")


if __name__ == "__main__":
    main()
