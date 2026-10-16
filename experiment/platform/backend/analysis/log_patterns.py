"""Stable message patterns without conflating error codes or destinations."""

import re


def log_pattern(message: str) -> str:
    text = re.sub(
        r"^\s*\[?\d{4}-\d{2}-\d{2}[T ][\d:.,]+(?:Z|[+-]\d{2}:?\d{2})?\]?\s*",
        "", message,
    )
    text = re.sub(
        r"(?i)\b(?:[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}|[a-f0-9]{16,64})\b",
        "<id>", text,
    )
    text = re.sub(
        r"(?i)(\b(?:request[_-]?id|trace[_-]?id|span[_-]?id|correlation[_-]?id)\s*[:=]\s*)[\w-]+",
        r"\1<id>", text,
    )
    return " ".join(text.split())
