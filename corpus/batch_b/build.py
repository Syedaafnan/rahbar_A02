#!/usr/bin/env python3
"""Regenerate the derived Batch B files (A03 Part A), deterministically:

    python corpus/batch_b/build.py

  b02  byte-identical copy of b01           -> uniqueness: exact duplicate
  b04  byte-identical copy of b03           -> uniqueness: exact duplicate
  b06  a 3-page PDF cut off before its end  -> completeness: truncated (and unreadable)
  b11  an OCR dump of a bad scan            -> quality: garbled

The hand-written files (b01, b03, b05, b07, b08, b09, b10, b12) and _batch.yaml are
edited directly. Every file is a synthetic test document, not a university document.
"""

from __future__ import annotations

import random
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent


def make_pdf(pages: list[list[str]], title: str) -> bytes:
    """A small valid PDF: one text block per page, Helvetica, with an info dict."""
    objs: list[bytes] = []
    n = len(pages)
    kids = " ".join(f"{5 + 2 * i} 0 R" for i in range(n))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    objs.append(f"<< /Title ({title}) /CreationDate (D:20261101000000) >>".encode())
    for i, lines in enumerate(pages):
        ops = ["BT /F1 11 Tf 14 TL 60 740 Td"]
        for line in lines:
            safe = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            ops.append(f"({safe}) Tj T*")
        ops.append("ET")
        stream = "\n".join(ops).encode("latin-1")
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources "
                    f"<< /Font << /F1 3 0 R >> >> /Contents {6 + 2 * i} 0 R >>".encode())
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    body = b"%PDF-1.4\n"
    offsets = []
    for number, obj in enumerate(objs, start=1):
        offsets.append(len(body))
        body += f"{number} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(body)
    body += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    body += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    body += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R /Info 4 0 R >>\n"
             f"startxref\n{xref}\n%%EOF\n").encode()
    return body


TRANSPORT = [
    ["Transport Policy",
     "Synthetic test document for CS4032 Assignment 03 (Batch B).",
     "It is not an official FAST-NUCES document.",
     "",
     "1. University buses run on fixed routes from 7:00 am every working day.",
     "2. A transport card is issued per semester after the transport fee is paid.",
     "3. Students must show the card when boarding; a lost card costs Rs. 500."],
    ["4. Routes are revised at the start of each fall semester.",
     "5. Requests for a new stop are sent to the transport office in writing.",
     "6. Buses leave the campus at 4:30 pm and 6:00 pm after classes."],
    ["7. Smoking and loud music are not allowed in university buses.",
     "8. Damage to a bus is charged to the students responsible.",
     "9. The transport fee is not refunded after the second week of classes."],
]


def garbled_scan(seed: int = 7) -> str:
    """What Tesseract returns for a skewed, low-contrast scan: symbols and fragments."""
    rnd = random.Random(seed)
    noise = list("~#@%&*^|{}[]<>\\/=+_;:0123456789")
    lines = ["Synthetic test document for CS4032 Assignment 03 (Batch B): a failed OCR scan."]
    for _ in range(60):
        lines.append("".join(rnd.choice(noise) if rnd.random() < 0.7 else rnd.choice("aeiourstln ")
                             for _ in range(rnd.randint(30, 70))))
    return "\n".join(lines) + "\n"


def main() -> None:
    shutil.copyfile(HERE / "b01-library-borrowing-rules.md", HERE / "b02-library-borrowing-rules-copy.md")
    shutil.copyfile(HERE / "b03-laboratory-safety-policy.md", HERE / "b04-laboratory-safety-policy (1).md")
    full = make_pdf(TRANSPORT, "Transport Policy")
    cut = full[: int(len(full) * 0.55)]          # the download stopped half way
    (HERE / "b06-transport-policy.pdf").write_bytes(cut)
    (HERE / "b11-scanned-notice-ocr.txt").write_text(garbled_scan(), encoding="utf-8")
    print(f"b06: {len(cut)} of {len(full)} bytes kept; b02, b04, b11 written")


if __name__ == "__main__":
    main()
