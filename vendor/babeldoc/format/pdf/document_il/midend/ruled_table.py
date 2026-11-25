"""Recognise prose in ruled PDF cells before layout fragments are translated."""
from collections import Counter
import logging
import re
import statistics
import unicodedata

import pymupdf

from babeldoc.format.pdf.document_il import Box, PdfLine, PdfParagraph, PdfParagraphComposition


CELL_LABEL = "ruled_table_cell"
_LIST_PREFIX = re.compile(r"^(?:[•●◦▪‣]|[-–]\s|\(?\d+[.)]\s|\([a-zA-Z0-9]\)\s)")
_MATH_FONT = re.compile(r"math|symbol|dingbat|cm(?:mi|sy|ex)", re.I)
logger = logging.getLogger(__name__)


def _signature(text):
    return "".join(unicodedata.normalize("NFKC", text).split()).replace("\u00ad", "")


def _could_have_grid(page):
    """Quickly reject prose pages, charts with only one axis, and photos."""
    xs, ys = set(), set()
    for path in page.get_drawings():
        for item in path["items"]:
            if item[0] == "l":
                a, b = item[1:3]
                if abs(a.x-b.x) < 1 and abs(a.y-b.y) > 8:
                    xs.add(round(a.x))
                if abs(a.y-b.y) < 1 and abs(a.x-b.x) > 8:
                    ys.add(round(a.y))
            elif item[0] == "re":
                r = item[1]
                if r.width < 2 and r.height > 8:
                    xs.add(round((r.x0+r.x1)/2))
                elif r.height < 2 and r.width > 8:
                    ys.add(round((r.y0+r.y1)/2))
                elif "s" in path["type"] and min(r.width, r.height) > 8:
                    xs.update((round(r.x0), round(r.x1)))
                    ys.update((round(r.y0), round(r.y1)))
        if len(xs) >= 3 and len(ys) >= 3:
            return True
    return False


def _source_groups(words, cell):
    """Group positioned words by their visual line, then join soft wraps."""
    lines = []
    for w in sorted(words, key=lambda w: ((w[1]+w[3])/2, w[0])):
        mid = (w[1]+w[3])/2
        if not lines or mid - lines[-1][0] > .40 * (w[3]-w[1]):
            lines.append((mid, []))
        lines[-1][1].append(w)
    groups = []
    previous_baseline = None
    previous_right = cell.x1
    previous_start = cell.x0
    for baseline, row in lines:
        row.sort(key=lambda w: w[0])
        text = " ".join(w[4] for w in row)
        height = statistics.median(w[3]-w[1] for w in row)
        first_line_indent = (row[0][0] - previous_start > height and
                             previous_right < cell.x1 - 2*height and
                             groups and groups[-1][1].endswith((".", "!", "?", "。", "？", "！")))
        if not groups or _LIST_PREFIX.match(text) or baseline-previous_baseline > 1.52*height or first_line_indent:
            groups.append([min(w[1] for w in row), text])
        else:
            prev = groups[-1][1]
            # Retain visible lexical hyphens (e.g. pre-defined); remove a
            # discretionary soft hyphen and avoid introducing a second space.
            if prev.endswith("\u00ad"):
                groups[-1][1] = prev[:-1] + text
            elif prev.endswith("-"):
                groups[-1][1] = prev + text
            elif re.search(r"[\u3000-\u9fff]$", prev) and re.match(r"[\u3000-\u9fff]", text):
                groups[-1][1] = prev + text
            else:
                groups[-1][1] = prev + " " + text
        previous_baseline = baseline
        previous_start, previous_right = row[0][0], row[-1][2]
    return groups


def _prose(text):
    return bool(re.search(r"[^\W\d_]{3,}", text, re.UNICODE))


def _cell_paragraphs(cell, words, chars, page_height, font_names, cell_id):
    if not chars or any(c.vertical or not c.pdf_style or not c.pdf_style.font_size for c in chars):
        return []
    if len({c.xobj_id for c in chars}) != 1 or chars[0].xobj_id is None:
        return []
    visible = [c for c in chars if (c.char_unicode or "").strip()]
    if not visible:
        return []
    largest_size = max(c.pdf_style.font_size for c in visible)
    if any(_MATH_FONT.search(font_names.get(c.pdf_style.font_id, "")) for c in visible):
        return []
    small = [c for c in visible if c.pdf_style.font_size < .85 * largest_size]
    if small:
        # A few smaller glyphs inside an alphanumeric measurement (HRmax,
        # VO2max, SpO2) still belong to the same sentence. Leave equations,
        # mixed-size headings, and detached notes to the formula pipeline.
        for c in small:
            x, y = (c.box.x + c.box.x2)/2, page_height-(c.box.y+c.box.y2)/2
            if not any(w[0]-.5 <= x <= w[2]+.5 and w[1]-.5 <= y <= w[3]+.5
                       and re.fullmatch(r"[(]?[A-Z][A-Za-z\d]{1,10}[),.]?", w[4])
                       for w in words):
                return []
    groups = _source_groups(words, cell)
    source = " ".join(g[1] for g in groups)
    if not _prose(source) or "\ufffd" in source or re.search(r"[∫∑∏√∂∇∈∉]", source):
        return []
    # Only replace glyphs when the positioned source extraction accounts for
    # all their characters. Keep unusual encodings and inline math untouched.
    if Counter(_signature(source)) != Counter(_signature("".join(c.char_unicode or "" for c in chars))):
        return []
    paragraphs = []
    left_padding = min(5., max(1., min(w[0] for w in words)-cell.x0))
    top_padding = min(2., max(.5, groups[0][0]-cell.y0))
    for n, (top, text) in enumerate(groups):
        next_top = groups[n+1][0] if n+1 < len(groups) else cell.y1
        span = [c for c in chars if top-.8 <= page_height-(c.box.y+c.box.y2)/2 < next_top-.8]
        if not span:
            return []
        if Counter(_signature(text)) != Counter(_signature("".join(c.char_unicode or "" for c in span))):
            return []
        style = Counter((c.pdf_style.font_id, round(c.pdf_style.font_size, 2)) for c in span).most_common(1)[0][0]
        base_style = next(c.pdf_style for c in span if (c.pdf_style.font_id, round(c.pdf_style.font_size, 2)) == style)
        box = Box(cell.x0+left_padding, page_height-next_top+.5,
                  cell.x1-min(left_padding, 2.), page_height-max(cell.y0+top_padding, top))
        if box.x2-box.x <= 1 or box.y2-box.y <= 1:
            return []
        paragraphs.append(PdfParagraph(
            box=box, pdf_style=base_style, xobj_id=span[0].xobj_id, unicode=text, vertical=False,
            first_line_indent=False, layout_label=CELL_LABEL, debug_id=f"cell-{cell_id}-{n+1}",
            render_order=min((c.render_order for c in span if c.render_order is not None), default=0),
            pdf_paragraph_composition=[PdfParagraphComposition(pdf_line=PdfLine(box=box, pdf_character=span))],
        ))
    return paragraphs


def take_ruled_cells(page, pdf_page):
    """Collect strictly bounded cells and detach only matching glyphs from IR."""
    if not page.pdf_character or pdf_page.rotation or not _could_have_grid(pdf_page):
        return []
    h = pdf_page.rect.height
    tables = pdf_page.find_tables(strategy="lines_strict").tables
    if not tables:
        return []
    all_words = pdf_page.get_text("words")
    image_boxes = [pymupdf.Rect(info["bbox"]) for info in pdf_page.get_image_info()]
    figure_boxes = [pymupdf.Rect(l.box.x, h-l.box.y2, l.box.x2, h-l.box.y)
                    for l in page.page_layout if l.box and l.class_name in
                    ("figure", "chart", "image", "equation", "formula", "isolate_formula")]
    font_names = {f.font_id: f.name or "" for f in page.pdf_font}
    owned = set()
    paragraphs = []
    # Word and glyph centres are in opposite vertical coordinate systems.
    # Spatial buckets limit the work on dense tables instead of scanning the
    # entire page for every individual cell.
    bucket_size = 32
    def bucket(items):
        groups = {}
        for x, y, obj in items:
            groups.setdefault((int(x//bucket_size), int(y//bucket_size)), []).append((x,y,obj))
        return groups
    def inside(groups, rect):
        return [obj for by in range(int(rect.y0//bucket_size),int(rect.y1//bucket_size)+1)
                for bx in range(int(rect.x0//bucket_size),int(rect.x1//bucket_size)+1)
                for x,y,obj in groups.get((bx,by), ()) if rect.x0<=x<rect.x1 and rect.y0<=y<rect.y1]
    word_index = bucket(((w[0]+w[2])/2,(w[1]+w[3])/2,w) for w in all_words)
    char_index = bucket(((c.box.x+c.box.x2)/2,h-(c.box.y+c.box.y2)/2,c)
                        for c in page.pdf_character if c.box)
    for t, table in enumerate(tables):
        if table.row_count < 2 or table.col_count < 2:
            continue
        table_box = pymupdf.Rect(table.bbox)
        if any((table_box & fig).get_area() > .85*table_box.get_area() for fig in figure_boxes):
            continue
        rectangles = [pymupdf.Rect(rect) for rect in table.cells if rect]
        content = [(r,inside(word_index,r)) for r in rectangles]
        if sum(any(_prose(w[4]) for w in words) for _,words in content) < 2:
            continue
        # A graph grid generally has ticks and numbers, not prose in boxes.
        if not any(sum(_prose(w[4]) for w in words)>=3 for _,words in content):
            continue
        for k, (rect, words) in enumerate(content):
            if not words or any(rect.intersects(image) for image in image_boxes):
                continue
            if any(w[0]<rect.x0-1 or w[2]>rect.x1+1 for w in words):
                continue
            chars = inside(char_index, rect)
            if any(id(c) in owned for c in chars):
                continue
            cells = _cell_paragraphs(rect, words, chars, h, font_names, f"{t+1}-{k+1}")
            if cells:
                paragraphs.extend(cells)
                owned.update(id(c) for c in chars)
    if paragraphs:
        page.pdf_character = [c for c in page.pdf_character if id(c) not in owned]
        logger.info("Reconstructed %d ruled-table text blocks on page %d", len(paragraphs), page.page_number+1)
    return paragraphs
