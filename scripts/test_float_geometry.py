"""Pure geometry checks for floating windows; no desktop or network needed."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from window_chrome import clamp_bounds, scaled_size


def main():
    assert clamp_bounds((1450, 700, 400, 248), (0, 0, 1920, 1040), (360, 220)) == (1450, 700, 400, 248)
    assert clamp_bounds((1850, 1000, 400, 248), (0, 0, 1920, 1040), (360, 220)) == (1520, 792, 400, 248)
    assert clamp_bounds((-2500, -300, 400, 248), (-1920, 0, 0, 1040), (360, 220)) == (-1920, 0, 400, 248)
    assert clamp_bounds((1900, 1000, 1200, 900), (0, 0, 800, 600), (980, 720)) == (0, 0, 800, 600)
    assert scaled_size((400, 248), 96, 144) == (600, 372)
    assert scaled_size((600, 372), 144, 96) == (400, 248)
    for work in [(0, 0, 1920, 1040), (-1920, -1200, 0, -160), (100, 50, 900, 650)]:
        for bounds in [(-5000, -5000, 400, 248), (5000, 5000, 400, 248), (100, 100, 20, 20), (50, 50, 5000, 5000)]:
            x, y, width, height = clamp_bounds(bounds, work, (360, 220))
            assert x >= work[0] and y >= work[1]
            assert x+width <= work[2] and y+height <= work[3]
    print("PASS: floating bounds fit work areas, negative monitors, minimum sizing, and DPI size conversion.")


if __name__ == "__main__":
    main()
