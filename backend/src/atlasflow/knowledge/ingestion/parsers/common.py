from __future__ import annotations


def normalize_extracted_text(text: str) -> str:
    """Apply the same lossless line-ending/NUL cleanup to every source format."""
    return text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
