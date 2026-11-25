"""Read a synthetic chart with the reader and export its paginated notes."""
import asyncio
import base64
import io
import json
import multiprocessing
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "vendor")]


def run(folder, live=False):
    import pymupdf
    from PIL import Image, ImageDraw, ImageFont
    from image_translation import validate_crop, _notes_pdf, append_image_translations
    from reader_service import ReaderService
    image = Image.new("RGB", (720, 160), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 32)
    draw.text((20, 25), "Heart rate (bpm)", font=font, fill="black")
    draw.text((20, 85), "Recovery time (min)", font=font, fill="black")
    output = io.BytesIO()
    image.save(output, format="PNG")
    picture = output.getvalue()
    path = folder / "paper.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_image(pymupdf.Rect(40, 80, 550, 195), stream=picture)
        pdf.save(path)
    crop = "data:image/png;base64," + base64.b64encode(picture).decode()
    assert validate_crop(crop) == crop
    for invalid in ("https://example.com/test.png", "data:image/png;base64,broken", crop.replace("image/png","image/jpeg")):
        try:
            validate_crop(invalid)
        except ValueError:
            continue
        raise AssertionError("invalid crop accepted")
    if live:
        reader = ReaderService(folder)
        try:
            document = reader.open_file(path)["document"]
            payload = {"document_id":document["id"], "request_id":1, "engine":"indextranslate",
                       "lang_in":"en","lang_out":"zh","image_data":crop}
            result = reader.translate_image(payload)
            assert result["ok"] and "心率" in result["translation"], result.get("error")
            payload["request_id"] = 2
            assert reader.translate_image(payload)["cache_hit"]
        finally:
            reader.close()
        paths = asyncio.run(append_image_translations(path, {"mono":str(path)}, None, "en", "zh", folder / "figures", print))
        with pymupdf.open(paths["mono"]) as pdf:
            assert len(pdf) >= 2 and "心率" in pdf[-1].get_text()
            pdf[-1].get_pixmap().save(folder / "image-notes-preview.png")
        print("PASS live: selected region, warm image cache and indexed PDF appendix")
    else:
        replies = []
        def translate(value):
            validate_crop(value)
            replies.append(1)
            return "心率（次/分钟）\n恢复时间（分钟）"
        for n in (1, 2):
            notes = folder / f"notes-{n}.pdf"
            assert _notes_pdf(path, "1", "en", "zh", folder / "cache", notes, lambda msg: None, translate)
            with pymupdf.open(notes) as pdf:
                assert "心率" in pdf[0].get_text()
        assert len(replies) == 1
        print("PASS offline: invalid crops rejected, PDF figure extraction, cache reuse and CJK label layout")


if __name__ == "__main__":
    multiprocessing.freeze_support()
    data = ROOT / "test-data" / "image-acceptance"
    data.mkdir(parents=True, exist_ok=True)
    os.environ["PDFMT_DATA_DIR"] = str(data)
    os.environ["PDFMT_CACHE_DIR"] = str(data / "cache" / "babeldoc")
    if "--live" in sys.argv:
        folder = Path(tempfile.mkdtemp(dir=data))
        run(folder, True)
        print(folder)
    else:
        with tempfile.TemporaryDirectory(dir=data) as tmp:
            run(Path(tmp))
