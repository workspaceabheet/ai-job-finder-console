import hashlib
import io
import re
import unicodedata
from pathlib import Path

import docx
import pymupdf

EXTRACTOR_VERSION = "v1"
SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md"}

# Blocks whose top edges are within this many points are treated as the same
# visual row (used only to order blocks within a single column).
_ROW_TOLERANCE = 3.0


class UnsupportedFormatError(ValueError):
    """Raised when the uploaded file's extension/content isn't supported.
    .message is the exact user-facing rejection text."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def compute_hash(file_bytes: bytes) -> str:
    return hashlib.sha256(file_bytes).hexdigest()


def validate_extension(filename: str) -> str:
    """Returns the lowercased extension (e.g. '.pdf') if supported, else raises
    UnsupportedFormatError with a message naming the supported formats
    (satisfies AC3's 'specific error message' requirement)."""
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise UnsupportedFormatError(
            f"Unsupported file type '{ext or '(none)'}'. "
            "Supported formats: PDF (.pdf), Word (.docx), or plain text (.txt, .md)."
        )
    return ext


def extract_text(file_bytes: bytes, ext: str) -> str:
    """Dispatches to the right extractor by extension, then applies clean_text.
    Raises UnsupportedFormatError if ext isn't one of SUPPORTED_EXTENSIONS (should
    already be caught by validate_extension, kept here as a defensive guard)."""
    if ext not in SUPPORTED_EXTENSIONS:
        validate_extension(f"file{ext}")  # raises with the standard message
    if ext == ".pdf":
        raw = extract_pdf(file_bytes)
    elif ext == ".docx":
        raw = extract_docx(file_bytes)
    else:  # .txt / .md
        raw = extract_txt(file_bytes)
    return clean_text(raw)


def _split_columns(blocks: list, page_width: float) -> list[list]:
    """Groups text blocks into columns. A block narrower than half the page is
    assigned to the left or right column by its horizontal centre; wide blocks
    (headers spanning the page) break the flow into a new full-width band so
    that content above/below a spanning header stays in reading order."""
    mid = page_width / 2
    bands: list[list] = []  # each band: list of columns, each column list of blocks
    left: list = []
    right: list = []
    for b in sorted(blocks, key=lambda b: b[1]):
        x0, _y0, x1, _y1 = b[:4]
        width = x1 - x0
        spans_middle = x0 < mid - 5 and x1 > mid + 5
        if spans_middle and width > page_width * 0.5:
            if left or right:
                bands.append([left, right])
                left, right = [], []
            bands.append([[b]])
        elif (x0 + x1) / 2 < mid:
            left.append(b)
        else:
            right.append(b)
    if left or right:
        bands.append([left, right])

    ordered: list[list] = []
    for band in bands:
        for col in band:
            if col:
                ordered.append(col)
    return ordered


def _sort_column(col: list) -> list:
    """Top-to-bottom, then left-to-right within a vertical tolerance band."""
    return sorted(col, key=lambda b: (round(b[1] / _ROW_TOLERANCE), b[0]))


def extract_pdf(file_bytes: bytes) -> str:
    """PyMuPDF (fitz), block-aware: opens the doc, for each page calls
    page.get_text("blocks"), sorts blocks top-to-bottom then left-to-right
    within a vertical tolerance band (handles multi-column layouts), joins
    block text with double newlines between blocks, single newline within a
    block's internal lines."""
    pages_out: list[str] = []
    with pymupdf.open(stream=file_bytes, filetype="pdf") as doc:
        # PyMuPDF will happily open e.g. a PNG renamed to .pdf as an image
        # document; refuse anything that isn't really a PDF.
        if not doc.is_pdf:
            raise ValueError("not a PDF file")
        for page in doc:
            # block tuple: (x0, y0, x1, y1, text, block_no, block_type); type 0 = text
            blocks = [b for b in page.get_text("blocks") if b[6] == 0]
            texts: list[str] = []
            for col in _split_columns(blocks, page.rect.width):
                for b in _sort_column(col):
                    text = b[4].strip()
                    if text:
                        texts.append(text)
            pages_out.append("\n\n".join(texts))
    return "\n\n".join(p for p in pages_out if p)


def extract_docx(file_bytes: bytes) -> str:
    """python-docx: iterates document.paragraphs (joined with newlines) then
    every table's cells row-by-row (cells in a row joined with ' | ', rows
    joined with newlines), tables appended after paragraph text."""
    document = docx.Document(io.BytesIO(file_bytes))
    parts = ["\n".join(p.text for p in document.paragraphs)]
    for table in document.tables:
        rows = [" | ".join(cell.text for cell in row.cells) for row in table.rows]
        parts.append("\n".join(rows))
    return "\n\n".join(p for p in parts if p)


def extract_txt(file_bytes: bytes) -> str:
    """Decodes as UTF-8 (fallback latin-1 on UnicodeDecodeError) and returns
    as-is before clean_text does its pass."""
    try:
        return file_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return file_bytes.decode("latin-1")


_HYPHEN_BREAK = re.compile(r"(\w+)-\n(\w+)")
_PAGE_NUMBER_LINE = re.compile(r"^\s*\d{1,3}\s*$")
_EXCESS_BLANKS = re.compile(r"\n{4,}")  # 3+ blank lines == 4+ newlines
_HSPACE = re.compile(r"[ \t]+")


def clean_text(raw: str) -> str:
    """Light, deterministic only:
    1. Unicode-normalize (NFKC).
    2. Rejoin hyphenated line-breaks: regex r'(\\w+)-\\n(\\w+)' -> r'\\1\\2'.
    3. Strip standalone page-number lines: regex r'^\\s*\\d{1,3}\\s*$' per line.
    4. Collapse runs of 3+ blank lines to 2; collapse runs of spaces/tabs to one.
    No NLP-based stripping, no stopword removal, no stemming."""
    text = unicodedata.normalize("NFKC", raw)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _HYPHEN_BREAK.sub(r"\1\2", text)
    text = "\n".join(
        line for line in text.split("\n") if not _PAGE_NUMBER_LINE.match(line)
    )
    text = _HSPACE.sub(" ", text)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = _EXCESS_BLANKS.sub("\n\n\n", text)
    return text.strip()
