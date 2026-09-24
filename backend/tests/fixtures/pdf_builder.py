"""Minimal, hand-built PDF byte generator for tests (Phase 6).

No PDF-writing library is a project dependency (only `pypdf`, a *reader*) --
this builds the smallest valid PDF structure by hand, with correct byte
offsets computed programmatically rather than typed by hand, so it is
reliable rather than fragile. Used only by tests.
"""

from __future__ import annotations


def build_pdf(pages_text: list[str]) -> bytes:
    """A single- or multi-page PDF, one text line per page, extractable by
    `pypdf` (and therefore by `app.documents.parsers.pdf.parse_pdf`).
    """
    n_pages = len(pages_text)
    objects: list[bytes] = [b"<</Type/Catalog/Pages 2 0 R>>", _pages_object(n_pages)]

    page_obj_start = 3
    content_obj_start = page_obj_start + n_pages
    font_obj = content_obj_start + n_pages

    for i in range(n_pages):
        objects.append(_page_object(content_obj_start + i, font_obj))
    for text in pages_text:
        objects.append(_content_object(text))
    objects.append(b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>")

    return _assemble(objects)


def build_scanned_pdf(n_pages: int = 1) -> bytes:
    """A structurally valid PDF with pages but no text-showing operators at
    all -- the closest a hand-built fixture can get to a real scanned
    (image-only) PDF without an actual image, and enough to exercise
    `parse_pdf`'s "no usable text layer" detection.
    """
    return build_pdf([""] * n_pages)


def _pages_object(n_pages: int) -> bytes:
    kids = " ".join(f"{3 + i} 0 R" for i in range(n_pages))
    return f"<</Type/Pages/Kids[{kids}]/Count {n_pages}>>".encode()


def _page_object(content_obj: int, font_obj: int) -> bytes:
    return (
        f"<</Type/Page/Parent 2 0 R/Resources<</Font<</F1 {font_obj} 0 R>>>>"
        f"/MediaBox[0 0 612 792]/Contents {content_obj} 0 R>>"
    ).encode()


def _content_object(text: str) -> bytes:
    escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
    stream = f"BT /F1 24 Tf 100 700 Td ({escaped}) Tj ET".encode()
    return b"<</Length " + str(len(stream)).encode() + b">>\nstream\n" + stream + b"\nendstream"


def _assemble(objects: list[bytes]) -> bytes:
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj".encode() + b"\n" + obj + b"\nendobj\n"
    xref_offset = len(out)
    n = len(objects) + 1
    out += f"xref\n0 {n}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer<</Size {n}/Root 1 0 R>>\nstartxref\n{xref_offset}\n%%EOF".encode()
    return bytes(out)
