"""Build the Windows icon from the approved, unchanged ORCA v7 SVG."""
from io import BytesIO
from pathlib import Path
from PIL import Image, ImageDraw
import pymupdf

ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)
BACKPLATE = "#f5f5f5"


def render_mark(source, size=1536):
    """Rasterize the canonical vector; only remove transparent outer space."""
    with pymupdf.open(stream=Path(source).read_bytes(), filetype="svg") as document:
        page = document[0]
        scale = size / max(page.rect.width, page.rect.height)
        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=True)
        image = Image.open(BytesIO(pixmap.tobytes("png"))).convert("RGBA")
    bounds = image.getchannel("A").getbbox()
    if not bounds:
        raise ValueError("The ORCA source SVG contains no visible artwork")
    return image.crop(bounds)


def render_app_icon(source, size=1024):
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    pen = ImageDraw.Draw(image)
    inset = round(size * 0.03125)
    pen.rounded_rectangle((inset, inset, size-inset-1, size-inset-1),
                          radius=round(size * 0.21), fill=BACKPLATE)
    mark = render_mark(source)
    mark.thumbnail((round(size * 0.80), round(size * 0.80)), Image.Resampling.LANCZOS)
    image.alpha_composite(mark, ((size-mark.width)//2, (size-mark.height)//2))
    return image


def make_icon(destination=None):
    root = Path(__file__).resolve().parent.parent
    destination = Path(destination) if destination else root / "app.ico"
    source = root / "ui" / "assets" / "orca-mark-v7.svg"
    image = render_app_icon(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, format="ICO", sizes=[(size, size) for size in ICON_SIZES])
    image.resize((256, 256), Image.Resampling.LANCZOS).save(source.with_name("orca-app-icon.png"))
    return destination


if __name__ == "__main__":
    print(make_icon())
