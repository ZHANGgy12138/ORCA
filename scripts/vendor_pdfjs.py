"""Vendor pinned Mozilla PDF.js browser assets without npm install scripts."""
import base64
import hashlib
import io
import json
from pathlib import Path
import tarfile
import urllib.request

VERSION = "6.5.438"
URL = f"https://registry.npmjs.org/pdfjs-dist/-/pdfjs-dist-{VERSION}.tgz"
INTEGRITY = "vg8Pw1x+AFKy4AMDvhuWl5cOYV8d698UoEANoFFhc79hqXDnZQqnOXoxw3HG907WHOg1JYOvLhP756boddFxpA=="
ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "ui" / "vendor" / "pdfjs"


def main():
    import truststore
    truststore.inject_into_ssl()
    cache = ROOT / "build" / "downloads" / f"pdfjs-dist-{VERSION}.tgz"
    if cache.exists():
        raw = cache.read_bytes()
    else:
        with urllib.request.urlopen(URL, timeout=40) as response:
            raw = response.read(40 * 1024 * 1024)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(raw)
    if base64.b64encode(hashlib.sha512(raw).digest()).decode() != INTEGRITY:
        raise RuntimeError("PDF.js archive integrity mismatch; no assets installed")
    names = {"legacy/build/pdf.min.mjs": "build/pdf.mjs", "legacy/build/pdf.worker.min.mjs": "build/pdf.worker.mjs", "web/pdf_viewer.css": "web/pdf_viewer.css", "LICENSE": "LICENSE", "README.md": "README.md"}
    manifest = {"package": "pdfjs-dist", "version": VERSION, "url": URL, "integrity": "sha512-" + INTEGRITY, "build": "legacy minified browser build", "files": {}}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        for item in archive.getmembers():
            if not item.isfile() or not item.name.startswith("package/"):
                continue
            relative = item.name[len("package/"):]
            target_name = names.get(relative)
            if relative.startswith(("cmaps/", "standard_fonts/", "wasm/", "iccs/", "web/images/")):
                target_name = relative
            if not target_name or ".." in Path(target_name).parts:
                continue
            target = DEST / target_name
            target.parent.mkdir(parents=True, exist_ok=True)
            payload = archive.extractfile(item).read()
            target.write_bytes(payload)
            manifest["files"][target_name] = {"bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest(), "source": relative}
    for required in ("build/pdf.mjs", "build/pdf.worker.mjs", "web/pdf_viewer.css", "LICENSE"):
        if required not in manifest["files"]:
            raise RuntimeError("Missing PDF.js asset: " + required)
    (DEST / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    notices = ROOT / "licenses" / "pdfjs"
    notices.mkdir(parents=True, exist_ok=True)
    for file in DEST.rglob("*"):
        if file.is_file() and file.name.lower().startswith(("license", "notice")):
            target = notices / file.relative_to(DEST)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(file.read_bytes())
    (notices / "PROVENANCE.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Vendored PDF.js {VERSION}: {len(manifest['files'])} files, {sum(f['bytes'] for f in manifest['files'].values()) / 1048576:.2f} MiB")


if __name__ == "__main__":
    main()
