"""Check the cell pipeline with a synthetic PDF and an optional real table."""
import argparse
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "test-data" / "ruled-table"
DATA.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("PDFMT_DATA_DIR", str(DATA))
os.environ.setdefault("PDFMT_CACHE_DIR", str(DATA / "cache" / "babeldoc"))
sys.path[:0] = [str(ROOT / "vendor"), str(ROOT)]


def make_fixture(path):
    import pymupdf

    with pymupdf.open() as doc:
        page = doc.new_page(width=430, height=280)
        for top, bottom in [(35, 72), (72, 153), (153, 230)]:
            for left, right in [(30, 155), (155, 399)]:
                page.draw_rect((left, top, right, bottom), width=.5)
        page.insert_text((34, 52), "Method")
        page.insert_text((160, 52), "Protocol considerations")
        page.insert_text((34, 90), "1.1 Test population")
        for x, txt in ((160, "The"), (191, "device"), (235, "should"), (299, "be")):
            page.insert_text((x, 90), txt, fontsize=10)
        page.insert_text((160, 102), "placed according to the instructions.", fontsize=10)
        page.insert_text((34, 170), "2.2 Sample size")
        page.insert_text((160, 170), "1. Include at least 45 participants.", fontsize=10)
        page.insert_text((160, 185), "2. Record the mean heart rate (bpm).", fontsize=10)
        page.insert_text((160, 245), "Outside the table.")
        doc.save(path)


def parse_and_check(path, pages="1", real=False):
    import pymupdf
    from babeldoc.format.pdf.document_il.midend.ruled_table import take_ruled_cells
    from babeldoc.format.pdf.new_parser.native_parse import parse_with_native_text_only_to_legacy_ir

    doc = parse_with_native_text_only_to_legacy_ir(path, pages=pages, working_dir=str(DATA / "ir"))
    all_blocks = []
    with pymupdf.open(path) as source:
        for page in doc.page:
            before = {id(c) for c in page.pdf_character}
            if not real:
                from babeldoc.format.pdf.document_il import Box, PageLayout
                page.page_layout = [PageLayout(box=Box(0,0,430,280), class_name="figure")]
                assert not take_ruled_cells(page, source[page.page_number])
                assert before == {id(c) for c in page.pdf_character}
                page.page_layout = []
            cells = take_ruled_cells(page, source[page.page_number])
            all_blocks.extend(cells)
            moved = [id(c) for p in cells for c in p.pdf_paragraph_composition[0].pdf_line.pdf_character]
            assert len(moved) == len(set(moved))
            assert not set(moved).intersection(id(c) for c in page.pdf_character)
            # Space-only glyphs immediately next to a border can be discarded
            # when a text group commits, but all other source glyphs are owned.
            assert set(moved).issubset(before)
    texts = [p.unicode for p in all_blocks]
    if real:
        assert any("The index device should be placed according to manufacturer" in s and "instructions." in s for s in texts)
        assert any("Previous studies have indicated" in s and "heart rate (HR) by photoplethysmography (PPG)" in s for s in texts)
        assert any(s.startswith("Alternatively, manufacturers") for s in texts)
        assert any("HRmax" in s and "VO2max" in s and "W/rpm" in s for s in texts), texts
    else:
        assert "The device should be placed according to the instructions." in texts, texts
        assert len([s for s in texts if re.match(r"^[12]\. ", s)]) == 2
        assert "Outside the table." not in texts
    return texts


def offline_output(path):
    import pymupdf
    from babeldoc.format.pdf.high_level import translate
    from babeldoc.format.pdf.translation_config import TranslationConfig, WatermarkOutputMode
    from babeldoc.translator.translator import BaseTranslator

    class FixtureTranslator(BaseTranslator):
        name = "cell-test"

        def do_translate(self, text, rate_limit_params=None):
            if text.startswith("The device should be placed according"):
                return "应按照说明放置设备。"
            if "45 participants" in text:
                return "1. 至少纳入45名受试者。"
            if "mean heart rate" in text:
                return "2. 记录平均心率（bpm）。"
            return "表格栏目说明"

        def do_llm_translate(self, *args):
            raise NotImplementedError()

    config = TranslationConfig(translator=FixtureTranslator("en", "zh", True), input_file=path,
        lang_in="en", lang_out="zh", doc_layout_model=None, output_dir=DATA,
        watermark_output_mode=WatermarkOutputMode.NoWatermark, auto_extract_glossary=False,
        skip_scanned_detection=True, no_dual=True, pool_max_workers=1)
    result = translate(config)
    with pymupdf.open(result.mono_pdf_path) as doc:
        page = doc[0]
        for area, expected in [((155,72,399,153), "应按照说明放置设备。"),
                               ((155,153,399,230), "至少纳入45名受试者。")]:
            text = "".join(page.get_text(clip=pymupdf.Rect(area)).split())
            assert expected in text, text
            spans = [s for b in page.get_text("dict", clip=pymupdf.Rect(area))["blocks"] if "lines" in b for ln in b["lines"] for s in ln["spans"] if s["text"].strip()]
            assert spans and min(s["size"] for s in spans) >= 6.5
        assert page.get_text().count("应按照") == 1
        page.get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5)).save(DATA/"synthetic-result.png")
    return result.mono_pdf_path


def check_overflow():
    from types import SimpleNamespace
    from babeldoc.format.pdf.document_il import Box
    from babeldoc.format.pdf.document_il.midend.typesetting import Typesetting

    typesetter = object.__new__(Typesetting)
    typesetter.is_cjk = True
    checks = []
    bounds = Box(3, 3, 21, 14)
    def cannot_fit(units, box, scale, spacing, paragraph, wrap):
        assert box is bounds
        checks.append(scale)
        return [], False
    typesetter._layout_typesetting_units = cannot_fit
    paragraph = SimpleNamespace(box=bounds, pdf_style=SimpleNamespace(font_size=10),
                                layout_label="ruled_table_cell", debug_id="cell-test")
    try:
        typesetter._find_optimal_scale_and_layout(paragraph, SimpleNamespace(page_number=1),
                                                   [], apply_layout=True)
    except ValueError as exc:
        assert "空间不足" in str(exc)
    else:
        raise AssertionError("Overfull cell should request a readable alternative")
    assert .649 < min(checks) < .651


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--real", type=Path)
    parser.add_argument("--render", action="store_true", help="Requires the bundled font and layout resources in the isolated PDFMT_CACHE_DIR.")
    args = parser.parse_args()
    sample = DATA/"synthetic-table.pdf"
    make_fixture(sample)
    parse_and_check(sample)
    check_overflow()
    if args.real:
        print("Real matched cells and paragraphs:", len(parse_and_check(args.real, "1-2", real=True)))
    if args.render:
        print("Output verified", offline_output(sample))
    print("PASS: ruled cell boundaries, soft wraps, list items and outside text")
