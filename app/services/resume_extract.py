"""
resume_extract.py — turns an uploaded resume file into plain text so the
AI prompt (ai_brain._build_prompt) can actually read what's in it, instead
of only ever seeing the freeform about_paragraph.

Previously (see ai_brain.py's old LIMITATION note) a resume upload only
ever contributed its filename/title to the AI prompt — the bytes sat in
GridFS unread. This module closes that gap: called once at upload time
(profile.py:add_document), the extracted text is stored alongside the
document so it doesn't need re-extracting on every job application.

Kept deliberately small and dependency-light:
- PDF -> pypdf (pure Python, already common in this kind of stack, no
  system binary dependency like poppler/pdftotext would need on Render).
- DOCX -> python-docx.
- Anything else (txt, or a type we don't recognize) -> best-effort decode.

Extraction failures never raise — a resume that can't be parsed (scanned
image PDF, corrupt file, unsupported format) should degrade to "no text
available" rather than blocking the upload. The about_paragraph still
carries the user through in that case.
"""

import io

MAX_EXTRACTED_CHARS = 6000  # keeps the AI prompt bounded even for long resumes


def extract_text(content_type: str | None, filename: str | None, raw_bytes: bytes) -> str:
    """Best-effort synchronous extraction. Called from an async route handler
    but kept sync since pypdf/python-docx are both sync-only libraries and
    resumes are capped at 200KB (profile.py's MAX_RESUME_BYTES) — small
    enough that this doesn't need to be pushed to a thread pool."""
    name = (filename or "").lower()
    ctype = (content_type or "").lower()

    try:
        if ctype == "application/pdf" or name.endswith(".pdf"):
            return _extract_pdf(raw_bytes)
        if name.endswith(".docx") or "wordprocessingml" in ctype:
            return _extract_docx(raw_bytes)
        # Plain text / markdown / anything else short-form.
        return raw_bytes.decode("utf-8", errors="ignore")[:MAX_EXTRACTED_CHARS]
    except Exception:
        # Corrupt file, scanned/image-only PDF with no text layer, unknown
        # binary format, etc. — never block the upload over this.
        return ""


def _extract_pdf(raw_bytes: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(raw_bytes))
    parts = []
    for page in reader.pages:
        text = page.extract_text() or ""
        if text:
            parts.append(text)
        if sum(len(p) for p in parts) >= MAX_EXTRACTED_CHARS:
            break
    return "\n".join(parts)[:MAX_EXTRACTED_CHARS]


def _extract_docx(raw_bytes: bytes) -> str:
    import docx

    document = docx.Document(io.BytesIO(raw_bytes))
    parts = [p.text for p in document.paragraphs if p.text.strip()]
    return "\n".join(parts)[:MAX_EXTRACTED_CHARS]
