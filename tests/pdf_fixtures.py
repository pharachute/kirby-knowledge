"""Synthetic PDF fixtures for the Phase 5 tests -- **standard library only**.

Why hand-built PDFs instead of committed binaries:

* no extra dependency (no ReportLab / pypdf) is needed to produce them;
* the encodings that matter -- WinAnsi text, Type0/Identity-H with a ``ToUnicode`` CMap
  (so Chinese can be exercised), UTF-16BE metadata, RC4 standard security -- are explicit
  and reviewable;
* the automated tests then never depend on the network, on WSL/YourChar, or on private
  files of the user's machine (spec §十九).

Only pdfminer.six (the Phase 5 backend, already a declared dependency of the importer) is
used by the *tests* to read these files; the builders themselves import nothing.

All builders are also exercised against pdfminer in this project's own test suite, so a
broken fixture fails loudly instead of silently testing nothing.
"""

from __future__ import annotations

import hashlib
import struct

__all__ = [
    "build_latin_pdf",
    "build_cjk_pdf",
    "build_encrypted_pdf",
    "build_no_text_pdf",
    "truncate",
    "PAD",
]

#: Standard PDF password padding (PDF 32000-1, 7.6.3.3).
PAD = bytes(
    [
        0x28, 0xBF, 0x4E, 0x5E, 0x4E, 0x75, 0x8A, 0x41, 0x64, 0x00, 0x4E, 0x56, 0xFF, 0xFA, 0x01, 0x08,
        0x2E, 0x2E, 0x00, 0xB6, 0xD0, 0x68, 0x3E, 0x80, 0x2F, 0x0C, 0xA9, 0xFE, 0x64, 0x53, 0x69, 0x7A,
    ]
)


def _text_stream(lines: list[str], *, font_size: int = 14, x: int = 72, y: int = 720, leading: int = 18) -> bytes:
    """One ``BT``/``ET`` block drawing each line separately (so line breaks are layout)."""
    parts = [f"BT /F1 {font_size} Tf {x} {y} Td"]
    for index, line in enumerate(lines):
        if index:
            parts.append(f"0 -{leading} Td")
        parts.append("(" + _escape(line) + ") Tj")
    parts.append("ET")
    return " ".join(parts).encode("latin-1")


def _escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _pdf_string(value: str) -> bytes:
    """A PDF text string: latin-1 as a literal, otherwise UTF-16BE hex with a BOM."""
    try:
        return b"(" + _escape(value).encode("latin-1") + b")"
    except UnicodeEncodeError:
        return b"<FEFF" + value.encode("utf-16-be").hex().upper().encode("ascii") + b">"


def _pad(password: str) -> bytes:
    return (password.encode("latin-1") + PAD)[:32]


def _rc4(key: bytes, data: bytes) -> bytes:
    state = list(range(256))
    j = 0
    for i in range(256):
        j = (j + state[i] + key[i % len(key)]) & 0xFF
        state[i], state[j] = state[j], state[i]
    out = bytearray()
    i = j = 0
    for byte in data:
        i = (i + 1) & 0xFF
        j = (j + state[i]) & 0xFF
        state[i], state[j] = state[j], state[i]
        out.append(byte ^ state[(state[i] + state[j]) & 0xFF])
    return bytes(out)


class _Builder:
    """Minimal PDF writer: objects by id, a classic xref table and a trailer."""

    def __init__(self) -> None:
        self.objects: dict[int, bytes | None] = {}
        self._next = 1

    def reserve(self) -> int:
        oid = self._next
        self._next += 1
        self.objects[oid] = None
        return oid

    def add(self, body: bytes) -> int:
        oid = self.reserve()
        self.objects[oid] = body
        return oid

    def put(self, oid: int, body: bytes) -> None:
        self.objects[oid] = body

    def assemble(
        self,
        root: int,
        *,
        info: int | None = None,
        encrypt: int | None = None,
        id_bytes: bytes = b"0123456789abcdef",
    ) -> bytes:
        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets: dict[int, int] = {}
        for oid in sorted(self.objects):
            body = self.objects[oid]
            assert body is not None, f"object {oid} was reserved but never written"
            offsets[oid] = len(out)
            out += (f"{oid} 0 obj\n").encode("ascii") + body + b"\nendobj\n"
        xref_position = len(out)
        size = max(self.objects) + 1
        out += (f"xref\n0 {size}\n").encode("ascii") + b"0000000000 65535 f \n"
        for oid in range(1, size):
            offset = offsets.get(oid)
            out += (f"{offset:010d} 00000 n \n").encode("ascii") if offset is not None else b"0000000000 65535 f \n"
        trailer = f"<< /Size {size} /Root {root} 0 R"
        if info is not None:
            trailer += f" /Info {info} 0 R"
        if encrypt is not None:
            trailer += f" /Encrypt {encrypt} 0 R"
        trailer += f" /ID [<{id_bytes.hex()}> <{id_bytes.hex()}>] >>"
        out += b"trailer\n" + trailer.encode("ascii") + b"\nstartxref\n" + f"{xref_position}\n".encode("ascii")
        out += b"%%EOF\n"
        return bytes(out)


def _info_object(builder: _Builder, info: dict[str, str] | None) -> int | None:
    if not info:
        return None
    body = b"<< " + b" ".join(b"/" + key.encode("ascii") + b" " + _pdf_string(value) for key, value in info.items())
    return builder.add(body + b" >>")


def build_latin_pdf(
    pages: list[str],
    *,
    info: dict[str, str] | None = None,
    font_size: int = 14,
) -> bytes:
    """A text PDF using Helvetica/WinAnsiEncoding (ASCII/Latin-1 content)."""
    builder = _Builder()
    catalog = builder.reserve()
    pages_id = builder.reserve()
    font_id = builder.add(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    )
    kids: list[int] = []
    for text in pages:
        stream = _text_stream(text.split("\n"), font_size=font_size)
        content_id = builder.add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        kids.append(
            builder.add(
                (
                    "<< /Type /Page /Parent %d 0 R /MediaBox [0 0 612 792] "
                    "/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
                    % (pages_id, font_id, content_id)
                ).encode("ascii")
            )
        )
    builder.put(
        pages_id,
        ("<< /Type /Pages /Kids [" + " ".join(f"{k} 0 R" for k in kids) + f"] /Count {len(kids)} >>").encode("ascii"),
    )
    builder.put(catalog, (f"<< /Type /Catalog /Pages {pages_id} 0 R >>").encode("ascii"))
    return builder.assemble(catalog, info=_info_object(builder, info))


def build_cjk_pdf(pages: list[str], *, info: dict[str, str] | None = None) -> bytes:
    """A CJK PDF: Type0 / Identity-H font with a ToUnicode CMap (no font program needed).

    Each distinct character gets a CID; the CMap maps it back to the Unicode code point,
    which is exactly the path pdfminer.six uses to recover Chinese text.
    """
    builder = _Builder()
    catalog = builder.reserve()
    pages_id = builder.reserve()
    cmap_id = builder.reserve()
    descendant_id = builder.reserve()
    font_id = builder.reserve()
    descriptor_id = builder.reserve()

    characters: list[str] = []
    for text in pages:
        for character in text:
            if character not in characters:
                characters.append(character)
    cid_of = {character: index + 1 for index, character in enumerate(characters)}

    kids: list[int] = []
    for text in pages:
        hex_lines = ["".join(f"{cid_of[character]:04X}" for character in line) for line in text.split("\n")]
        parts = ["BT /F1 14 Tf 72 720 Td"]
        for index, hex_line in enumerate(hex_lines):
            if index:
                parts.append("0 -18 Td")
            parts.append(f"<{hex_line}> Tj")
        parts.append("ET")
        stream = " ".join(parts).encode("ascii")
        content_id = builder.add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        kids.append(
            builder.add(
                (
                    "<< /Type /Page /Parent %d 0 R /MediaBox [0 0 612 792] "
                    "/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
                    % (pages_id, font_id, content_id)
                ).encode("ascii")
            )
        )
    builder.put(
        pages_id,
        ("<< /Type /Pages /Kids [" + " ".join(f"{k} 0 R" for k in kids) + f"] /Count {len(kids)} >>").encode("ascii"),
    )
    builder.put(catalog, (f"<< /Type /Catalog /Pages {pages_id} 0 R >>").encode("ascii"))
    builder.put(
        descriptor_id,
        b"<< /Type /FontDescriptor /FontName /TestCJK /Flags 4 /FontBBox [0 -200 1000 900] "
        b"/ItalicAngle 0 /Ascent 800 /Descent -200 /CapHeight 700 /StemV 80 >>",
    )
    builder.put(
        descendant_id,
        (
            "<< /Type /Font /Subtype /CIDFontType0 /BaseFont /TestCJK "
            "/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
            f"/DW 1000 /FontDescriptor {descriptor_id} 0 R >>"
        ).encode("ascii"),
    )
    builder.put(
        font_id,
        (
            "<< /Type /Font /Subtype /Type0 /BaseFont /TestCJK /Encoding /Identity-H "
            f"/DescendantFonts [{descendant_id} 0 R] /ToUnicode {cmap_id} 0 R >>"
        ).encode("ascii"),
    )
    cmap_lines = [
        "/CIDInit /ProcSet findresource begin",
        "12 dict begin begincmap",
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
        "/CMapName /Adobe-Identity-UCS def",
        "/CMapType 2 def",
        "1 begincodespacerange",
        "<0000> <FFFF>",
        "endcodespacerange",
    ]
    pairs = [(f"<{cid_of[character]:04X}>", f"<{ord(character):04X}>") for character in characters]
    for start in range(0, len(pairs), 100):
        chunk = pairs[start : start + 100]
        cmap_lines.append(f"{len(chunk)} beginbfchar")
        cmap_lines += [f"{source} {target}" for source, target in chunk]
        cmap_lines.append("endbfchar")
    cmap_lines += ["endcmap", "CMapName currentdict /CMap defineresource pop", "end", "end"]
    cmap = "\n".join(cmap_lines).encode("ascii")
    builder.put(cmap_id, b"<< /Length %d >>\nstream\n" % len(cmap) + cmap + b"\nendstream")
    return builder.assemble(catalog, info=_info_object(builder, info))


def build_encrypted_pdf(
    pages: list[str],
    *,
    user_password: str = "",
    owner_password: str = "owner-password",
    permissions: int = -44,
    info: dict[str, str] | None = None,
) -> bytes:
    """A PDF with RC4 40-bit standard security (V1/R2).

    With a non-empty ``user_password`` the document needs a password (pdfminer raises
    ``PDFPasswordIncorrect`` when opened with the empty password).  With
    ``permissions=-64`` text extraction is forbidden while the document still opens, which
    exercises the permission branch of the importer.
    """
    id_bytes = b"0123456789abcdef"
    owner_entry = _rc4(hashlib.md5(_pad(owner_password)).digest()[:5], _pad(user_password))
    file_key = hashlib.md5(_pad(user_password) + owner_entry + struct.pack("<i", permissions) + id_bytes).digest()[:5]
    user_entry = _rc4(file_key, PAD)

    builder = _Builder()
    catalog = builder.reserve()
    pages_id = builder.reserve()
    font_id = builder.add(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    )
    kids: list[int] = []
    for text in pages:
        plain = _text_stream(text.split("\n"))
        # RC4 object key = MD5(file key + object number(3) + generation(2) + ID)[:10]
        content_id = builder.reserve()
        # per-object key: MD5(file key + object number(3) + generation(2))[:10]
        # (the /ID is only part of the *file* key derivation, never of the object key)
        object_key = hashlib.md5(file_key + struct.pack("<I", content_id)[:3] + b"\x00\x00").digest()[:10]
        stream = _rc4(object_key, plain)
        builder.put(content_id, b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        kids.append(
            builder.add(
                (
                    "<< /Type /Page /Parent %d 0 R /MediaBox [0 0 612 792] "
                    "/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
                    % (pages_id, font_id, content_id)
                ).encode("ascii")
            )
        )
    builder.put(
        pages_id,
        ("<< /Type /Pages /Kids [" + " ".join(f"{k} 0 R" for k in kids) + f"] /Count {len(kids)} >>").encode("ascii"),
    )
    builder.put(catalog, (f"<< /Type /Catalog /Pages {pages_id} 0 R >>").encode("ascii"))
    encrypt_id = builder.add(
        (
            "<< /Filter /Standard /V 1 /R 2 /O <%s> /U <%s> /P %d >>"
            % (owner_entry.hex().upper(), user_entry.hex().upper(), permissions)
        ).encode("ascii")
    )
    return builder.assemble(catalog, info=_info_object(builder, info), encrypt=encrypt_id, id_bytes=id_bytes)


def build_no_text_pdf(*, pages: int = 1) -> bytes:
    """A readable PDF with no text at all (stands in for a scanned/image-only page)."""
    builder = _Builder()
    catalog = builder.reserve()
    pages_id = builder.reserve()
    kids: list[int] = []
    for _ in range(pages):
        stream = b"0 0 200 200 re f"
        content_id = builder.add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        kids.append(
            builder.add(
                (
                    "<< /Type /Page /Parent %d 0 R /MediaBox [0 0 612 792] /Contents %d 0 R >>"
                    % (pages_id, content_id)
                ).encode("ascii")
            )
        )
    builder.put(
        pages_id,
        ("<< /Type /Pages /Kids [" + " ".join(f"{k} 0 R" for k in kids) + f"] /Count {len(kids)} >>").encode("ascii"),
    )
    builder.put(catalog, (f"<< /Type /Catalog /Pages {pages_id} 0 R >>").encode("ascii"))
    return builder.assemble(catalog)


def truncate(pdf: bytes, fraction: float) -> bytes:
    """Cut a PDF at ``fraction`` of its length (a truncated/corrupt file)."""
    if not 0 < fraction < 1:
        raise ValueError("fraction must be between 0 and 1")
    return pdf[: max(1, int(len(pdf) * fraction))]
