"""Text overlay rendered onto the luma (Y) plane of recorded frames.

Renders lines of text into a grayscale strip once (refreshed every few
frames), which is then cheaply blitted onto each frame's Y plane: the strip
region is darkened and text pixels pushed to white. Chroma is untouched, so
the overlay appears as white-on-dark regardless of scene content.
"""

import numpy as np
from PIL import Image, ImageDraw, ImageFont

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",   # Raspberry Pi OS
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Courier New.ttf",    # macOS (mock dev)
    "/System/Library/Fonts/Helvetica.ttc",
]


def _load_font(size):
    for path in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


class TextOverlay:
    def __init__(self, width, font_size=28, pad=8):
        self.width = width
        self.pad = pad
        self.font = _load_font(font_size)
        self.line_h = font_size + 6

    def render(self, lines) -> np.ndarray:
        """Render text lines into a (strip_h, width) uint8 array."""
        h = 2 * self.pad + self.line_h * len(lines)
        img = Image.new("L", (self.width, h), 0)
        draw = ImageDraw.Draw(img)
        for i, line in enumerate(lines):
            draw.text((self.pad, self.pad + i * self.line_h), line, fill=255, font=self.font)
        return np.asarray(img)

    @staticmethod
    def blit(y_plane: np.ndarray, strip: np.ndarray):
        """Burn a rendered strip into the top of a Y plane, in place."""
        h, w = strip.shape
        region = y_plane[:h, :w]
        region[:] = region // 3          # darken the band for contrast
        np.maximum(region, strip, out=region)
