from __future__ import annotations


def escape_untrusted_prompt_text(value: object) -> str:
    """Escape text before embedding it inside model-facing memory sections."""
    text = str(value or "")
    return text.replace("<", "&lt;").replace(">", "&gt;")
