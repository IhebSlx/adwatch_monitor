"""Erzeugt static/adwatch.ico fuer die Desktop-Verknuepfung.

Windows sucht sich aus einer .ico-Datei selbst die passende Groesse: 16 px in
der Taskleiste, 256 px in der grossen Kachelansicht. Deshalb liegen alle
Groessen in einer Datei -- eine einzelne 256er wuerde klein skaliert matschig.

Die Farbe ist --accent aus static/app.css, damit Symbol und App zusammenpassen.
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ACCENT = (79, 92, 229)          # --accent  #4f5ce5
TEXT = (255, 255, 255)
GROESSEN = [256, 128, 64, 48, 32, 16]
ZIEL = Path(__file__).resolve().parent.parent / "static" / "adwatch.ico"


def _schrift(px: int):
    """Erste Schrift, die es auf diesem Rechner gibt -- sonst die Standardschrift."""
    for name in ("segoeuib.ttf", "arialbd.ttf", "seguisb.ttf"):
        try:
            return ImageFont.truetype(name, px)
        except OSError:
            continue
    return ImageFont.load_default()


def kachel(px: int) -> Image.Image:
    # Viermal so gross zeichnen und dann verkleinern: das glaettet die runden
    # Ecken, die PIL sonst hart und ausgefranst zeichnet.
    f = 4
    img = Image.new("RGBA", (px * f, px * f), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, px * f - 1, px * f - 1],
                        radius=int(px * f * 0.22), fill=ACCENT)
    text = "AW" if px >= 32 else "A"
    schrift = _schrift(int(px * f * (0.42 if len(text) == 2 else 0.60)))
    l, t, r, b = d.textbbox((0, 0), text, font=schrift)
    d.text(((px * f - (r - l)) / 2 - l, (px * f - (b - t)) / 2 - t),
           text, font=schrift, fill=TEXT)
    return img.resize((px, px), Image.LANCZOS)


if __name__ == "__main__":
    bilder = [kachel(p) for p in GROESSEN]
    bilder[0].save(ZIEL, format="ICO",
                   sizes=[(p, p) for p in GROESSEN], append_images=bilder[1:])
    print("geschrieben:", ZIEL, ZIEL.stat().st_size, "Bytes")
