"""
Title rendering on generated pictures.

Image models misspell words and mangle Greek, so the tool draws the title
itself: a soft dark band at the bottom of the picture with the title in a
bold font that has Greek glyphs (DejaVu/FreeSans on Linux, Arial Bold on
Windows and Mac).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

log = logging.getLogger("overlay")
FONT_CANDIDATES = [
    os.getenv("VIDEO_FONT", ""),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
]
MAX_TITLE = 80


def _font(size: int):
    for path in FONT_CANDIDATES:
        if path and Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _wrap(draw, text: str, font, max_width: int) -> list[str]:
    words, lines, line = text.split(), [], ""
    for word in words:
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=font) <= max_width or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines[:4]


def render_title(path: Path, title: str, out: Path | None = None) -> Path:
    """Draw `title` on the lower part of the picture. Returns the written path
    (same file unless `out` is given)."""
    title = " ".join((title or "").split())[:MAX_TITLE]
    path = Path(path)
    out = Path(out) if out else path
    if not title:
        return path
    with Image.open(path) as src:
        img = src.convert("RGBA")
    w, h = img.size
    size = max(24, w // 16)
    font = _font(size)
    draw = ImageDraw.Draw(img)
    lines = _wrap(draw, title, font, int(w * 0.86))
    line_h = int(size * 1.25)
    block_h = line_h * len(lines) + int(size * 1.2)
    # soft dark band so the text reads on any picture
    band = Image.new("RGBA", (w, block_h + int(size * 1.5)), (0, 0, 0, 0))
    ImageDraw.Draw(band).rectangle([0, int(size * 0.6), w, band.height], fill=(0, 0, 0, 150))
    band = band.filter(ImageFilter.GaussianBlur(size // 3))
    img.alpha_composite(band, (0, h - band.height))
    draw = ImageDraw.Draw(img)
    y = h - block_h - int(size * 0.3)
    for line in lines:
        x = (w - draw.textlength(line, font=font)) / 2
        draw.text((x + 2, y + 2), line, font=font, fill=(0, 0, 0, 200))        # shadow
        draw.text((x, y), line, font=font, fill=(255, 255, 255, 255))
        y += line_h
    rgb = img.convert("RGB")
    rgb.save(out, quality=92) if out.suffix.lower() in (".jpg", ".jpeg") else rgb.save(out)
    log.info("  title drawn on %s: %s", out.name, title)
    return out
