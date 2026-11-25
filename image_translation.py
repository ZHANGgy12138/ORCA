"""Translate figure labels on demand and append a page-indexed reading aid."""
import asyncio
import base64
import hashlib
import html
import io
import json
import multiprocessing
import os
from pathlib import Path
import tempfile

MAX_EDGE = 1600
MAX_BYTES = 3 * 1024 * 1024


def validate_crop(value):
    from PIL import Image
    if not isinstance(value, str) or len(value) > MAX_BYTES * 4 // 3 + 64:
        raise ValueError("框选图片过大，请缩小选区。")
    header, sep, encoded = value.partition(",")
    allowed = {"data:image/png;base64": "PNG", "data:image/jpeg;base64": "JPEG"}
    if not sep or header not in allowed:
        raise ValueError("图片选区格式无效。")
    try:
        raw = base64.b64decode(encoded, validate=True)
        if not 0 < len(raw) <= MAX_BYTES:
            raise ValueError()
        with Image.open(io.BytesIO(raw)) as image:
            if image.format != allowed[header] or max(image.size) > MAX_EDGE:
                raise ValueError()
            image.verify()
    except Exception:
        raise ValueError("图片选区无法读取，请缩小范围并重新框选。") from None
    return value


def selected_pages(selection, count):
    if not selection:
        return range(count)
    pages = set()
    for part in selection.split(","):
        bounds = part.split("-")
        start, end = int(bounds[0]), int(bounds[-1])
        if not 1 <= start <= end <= count:
            raise ValueError("图片翻译页码超出文档范围。")
        pages.update(range(start - 1, end))
    return sorted(pages)


def figures(page):
    import pymupdf as fitz
    # Image positions are unrotated; rendering clips are in displayed coordinates.
    seen = set()
    for info in page.get_image_info():
        rect = (fitz.Rect(info["bbox"]) * page.rotation_matrix) & page.rect
        if min(rect.width, rect.height) < 22 or rect.get_area() < 1800:
            continue
        box = tuple(round(v, 1) for v in rect)
        if box in seen:
            continue
        seen.add(box)
        zoom = min(2.5, (MAX_EDGE - 2) / max(rect.width, rect.height))
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=rect, colorspace=fitz.csRGB, alpha=False, annots=False)
        yield pix.tobytes("jpeg", jpg_quality=85)


def _notes_pdf(source, selection, lang_in, lang_out, directory, appendix_path, progress, translate):
    import pymupdf as fitz
    import sqlite3
    directory.mkdir(parents=True, exist_ok=True)
    cache = sqlite3.connect(directory / "figures.sqlite")
    writer = None
    try:
        cache.execute("CREATE TABLE IF NOT EXISTS labels (k TEXT PRIMARY KEY, reply TEXT NOT NULL)")
        with fitz.open(source) as doc:
            for number in selected_pages(selection, len(doc)):
                for ordinal, picture in enumerate(figures(doc[number]), 1):
                    progress(f"识别第 {number+1} 页 · 图片 {ordinal}")
                    digest = hashlib.sha256(b"index-35b-labels-v1" + json.dumps([lang_in, lang_out]).encode() + picture).hexdigest()
                    row = cache.execute("SELECT reply FROM labels WHERE k=?", (digest,)).fetchone()
                    if row:
                        reply = row[0]
                    else:
                        url = "data:image/jpeg;base64," + base64.b64encode(picture).decode("ascii")
                        reply = translate(url)
                        with cache:
                            cache.execute("INSERT OR REPLACE INTO labels VALUES (?,?)", (digest, reply))
                    if reply.strip() == "[NO_TEXT]":
                        continue
                    if writer is None:
                        writer = fitz.DocumentWriter(str(appendix_path))
                    title = f"图中文字译注　·　原文第 {number+1} 页 / 图片 {ordinal}"
                    archive = fitz.Archive()
                    archive.add((picture, "figure.jpg"))
                    from PIL import Image
                    with Image.open(io.BytesIO(picture)) as fig:
                        width = int(min(440, 210 * fig.width / fig.height))
                    body = "<h3>" + title + "</h3><img src='figure.jpg' width='" + str(width) + "'/><p>" + html.escape(reply).replace("\n", "<br/>") + "</p>"
                    story = fitz.Story(html=body, user_css="body{font-family:sans-serif;font-size:12pt;}p{line-height:1.6}",archive=archive)
                    story.write(writer, lambda n, filled: (fitz.paper_rect("a4"), fitz.Rect(42, 42, 553, 792), None))
    finally:
        if writer is not None:
            writer.close()
        cache.close()
    return writer is not None


def _append_job(source, outputs, selection, lang_in, lang_out, directory, pipe):
    import pymupdf as fitz
    from reader_service import _worker_factory
    from engine_registry import service_error
    try:
        translator = _worker_factory({"engine":"indextranslate","lang_in":lang_in,"lang_out":lang_out})()
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=directory) as temporary:
            appendix = Path(temporary) / "labels.pdf"
            found = _notes_pdf(source, selection, lang_in, lang_out, directory, appendix,
                               lambda msg: pipe.send(("stage", msg)), translator.translate_image)
            if found:
                updated = {}
                with fitz.open(appendix) as labels:
                    for kind, value in outputs.items():
                        original = Path(value)
                        final = original.with_name(original.stem + ".illustrated.pdf")
                        with fitz.open(original) as doc:
                            doc.insert_pdf(labels)
                            fd, filename = tempfile.mkstemp(dir=original.parent, suffix=".tmp")
                            os.close(fd)
                            try:
                                doc.save(filename, deflate=True)
                                os.replace(filename, final)
                            finally:
                                Path(filename).unlink(missing_ok=True)
                        updated[kind] = str(final)
                outputs = updated
        pipe.send(("done", outputs))
    except Exception as exc:
        pipe.send(("error", service_error(exc)))
    finally:
        pipe.close()


async def append_image_translations(source_path, outputs, selection, lang_in, lang_out, cache_dir, progress_callback):
    context = multiprocessing.get_context("spawn")
    rx, tx = context.Pipe(duplex=False)
    proc = context.Process(target=_append_job, args=(source_path, outputs, selection, lang_in, lang_out, cache_dir, tx), daemon=True)
    try:
        proc.start()
        tx.close()
        while True:
            if rx.poll():
                kind, value = rx.recv()
                if kind == "done":
                    return value
                if kind == "error":
                    raise RuntimeError(value)
                progress_callback(value)
            elif not proc.is_alive():
                raise RuntimeError("图片翻译进程提前退出。")
            await asyncio.sleep(.08)
    finally:
        tx.close()
        rx.close()
        if proc.pid:
            proc.join(.2)
            if proc.is_alive():
                proc.terminate()
                proc.join(2)
            if proc.is_alive():
                proc.kill()
                proc.join()
        proc.close()
