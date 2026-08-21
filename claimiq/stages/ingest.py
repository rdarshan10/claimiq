"""Ingestion: files -> normalised Documents with a page map.

The page map matters more than it looks: every citation downstream references
(doc_id, page), so page fidelity here is what makes "click the flag, land on
the source line" work at the UI layer.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from claimiq.core.orchestrator import Context
from claimiq.core.schemas import Document, Page

TEXT_SUFFIXES = {".txt", ".md", ".text"}
PDF_SUFFIXES = {".pdf"}

# Text packs use an explicit marker; otherwise fall back to form feeds.
_PAGE_MARK = re.compile(r"\[page\s+(\d+)\]", re.IGNORECASE)


def _doc_id(path: Path) -> str:
    h = hashlib.sha1(path.name.encode("utf-8")).hexdigest()[:6]
    return f"{path.stem[:28]}#{h}"


def _paginate_text(text: str, chars_per_page: int = 3000) -> list[Page]:
    """Split flat text into pages. Prefers explicit markers, then form feeds."""
    if _PAGE_MARK.search(text):
        parts = _PAGE_MARK.split(text)
        pages: list[Page] = []
        for i in range(1, len(parts), 2):
            pages.append(Page(number=int(parts[i]), text=parts[i + 1].strip()))
        if pages:
            return pages

    if "\f" in text:
        return [
            Page(number=i, text=chunk.strip())
            for i, chunk in enumerate(text.split("\f"), start=1)
            if chunk.strip()
        ]

    if len(text) <= chars_per_page:
        return [Page(number=1, text=text.strip())]

    # Paginate on paragraph boundaries so citations don't straddle pages.
    pages, buf, n = [], [], 1
    for para in text.split("\n\n"):
        if sum(len(b) for b in buf) + len(para) > chars_per_page and buf:
            pages.append(Page(number=n, text="\n\n".join(buf).strip()))
            buf, n = [], n + 1
        buf.append(para)
    if buf:
        pages.append(Page(number=n, text="\n\n".join(buf).strip()))
    return pages


def _read_pdf(path: Path) -> list[Page]:
    try:
        import pdfplumber
    except ImportError:
        return _read_pdf_fallback(path)

    pages: list[Page] = []
    try:
        with pdfplumber.open(path) as pdf:
            for i, page in enumerate(pdf.pages, start=1):
                text = page.extract_text() or ""
                # Tables carry the invoice line items; extract them explicitly
                # because flat text extraction mangles column alignment.
                for table in page.extract_tables() or []:
                    rows = [
                        " | ".join(str(c or "").strip() for c in row) for row in table
                    ]
                    if rows:
                        text += "\n[table]\n" + "\n".join(rows)
                pages.append(Page(number=i, text=text.strip()))
    except Exception:  # noqa: BLE001 - fall back rather than fail the claim
        return _read_pdf_fallback(path)
    return pages or _read_pdf_fallback(path)


def _read_pdf_fallback(path: Path) -> list[Page]:
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        return [
            Page(number=i, text=(p.extract_text() or "").strip())
            for i, p in enumerate(reader.pages, start=1)
        ]
    except Exception:  # noqa: BLE001
        return [Page(number=1, text="")]


def load_document(path: Path) -> Document:
    path = Path(path)
    suffix = path.suffix.lower()

    if suffix in PDF_SUFFIXES:
        pages = _read_pdf(path)
    elif suffix in TEXT_SUFFIXES:
        pages = _paginate_text(path.read_text(encoding="utf-8", errors="replace"))
    else:
        # Unknown binary (image evidence etc.) — register it so completeness
        # checks can see it exists, even with no extractable text.
        pages = [Page(number=1, text="")]

    return Document(doc_id=_doc_id(path), filename=path.name, pages=pages)


def load_claim_folder(folder: Path) -> list[Document]:
    folder = Path(folder)
    files = sorted(p for p in folder.iterdir() if p.is_file() and not p.name.startswith("."))
    return [load_document(p) for p in files]


class IngestStage:
    name = "ingest"

    def __init__(self, folder: Path) -> None:
        self.folder = Path(folder)

    def run(self, ctx: Context) -> None:
        docs = load_claim_folder(self.folder)
        ctx.result.documents = docs
        empty = [d.filename for d in docs if d.char_count() == 0]
        ctx.log(
            self.name,
            "event",
            documents=len(docs),
            pages=sum(d.page_count for d in docs),
            chars=sum(d.char_count() for d in docs),
            empty_documents=empty,
        )
        ctx.progress(self.name, f"{len(docs)} documents, {sum(d.page_count for d in docs)} pages")
