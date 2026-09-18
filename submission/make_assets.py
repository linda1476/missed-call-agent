"""Generate submission binary assets with stdlib only:

  slides.pdf — draft slide deck (one text page per slide)
  cover.png  — cover image (flat brand panel + title bars)

Run:  python submission/make_assets.py
"""

import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent

SLIDES = [
    "missed-call-agent",
    "Problem: small shops miss ~28-62% of calls; ~85% of those callers never call back",
    "Demo: first call -> booking; repeat call -> recalled; concurrent race -> 1 wins",
    "Architecture: Voice Agent API tool.call -> 3-kind memory -> CAS slot lock",
    "Differentiators: cross-call memory | concurrent slot lock | owner-correction rules",
    "Business: $4.50/hr all-in voice stack; pays for itself in saved bookings",
]


def _pdf_escape(s: str) -> str:
    return s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def make_pdf(path: Path, slides: list[str]) -> None:
    """Minimal single-font PDF, one slide per page."""
    objects: list[bytes] = []

    def add(body: str) -> int:
        objects.append(body.encode("latin-1"))
        return len(objects)

    font = add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    pages_kids = []
    for i, text in enumerate(slides):
        content = (f"BT /F1 22 Tf 60 700 Td ({_pdf_escape(text)}) Tj ET")
        content_id = add(
            f"<< /Length {len(content)} >>\nstream\n{content}\nendstream")
        page_id = add(
            f"<< /Type /Page /Parent 0 0 R /MediaBox [0 0 792 612] "
            f"/Resources << /Font << /F1 {font} 0 R >> >> "
            f"/Contents {content_id} 0 R >>")
        pages_kids.append(f"{page_id} 0 R")
    pages_id = add(
        f"<< /Type /Pages /Kids [{' '.join(pages_kids)}] /Count {len(pages_kids)} >>")
    catalog_id = add(f"<< /Type /Catalog /Pages {pages_id} 0 R >>")

    # Fix /Parent refs now that pages_id is known.
    for i, obj in enumerate(objects):
        objects[i] = obj.replace(b"/Parent 0 0 R",
                                 f"/Parent {pages_id} 0 R".encode())

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects)+1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objects)+1} /Root {catalog_id} 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n").encode()
    path.write_bytes(bytes(out))


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def make_png(path: Path, w: int = 1200, h: int = 630) -> None:
    """Flat dark cover with a green 'answered call' band — text-free, so the
    image stays valid without font deps; title goes in the listing fields."""
    rows = bytearray()
    for y in range(h):
        rows.append(0)  # filter byte
        for x in range(w):
            if 240 < y < 390 and 80 < x < w - 80:
                r, g, b = (47, 191, 113) if y < 330 else (24, 128, 76)
            else:
                r, g, b = 15, 17, 21
            if 100 < y < 200 and 100 < x < 500:
                r, g, b = 76, 141, 255
            rows += bytes((r, g, b))
    png = (b"\x89PNG\r\n\x1a\n"
           + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + _png_chunk(b"IDAT", zlib.compress(bytes(rows), 6))
           + _png_chunk(b"IEND", b""))
    path.write_bytes(png)


if __name__ == "__main__":
    make_pdf(OUT / "slides.pdf", SLIDES)
    make_png(OUT / "cover.png")
    print("wrote slides.pdf and cover.png")
