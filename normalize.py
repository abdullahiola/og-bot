"""Text normalization and timestamp helpers — port of normalize.ts."""

import re


def normalize(s: str) -> str:
    """Lowercase, collapse separators to spaces, strip."""
    out = s.lower().strip()
    out = re.sub(r"[_\-.]", " ", out)
    out = re.sub(r"\s+", " ", out)
    return out


def dex_pair_created_ms(t: int | float | None) -> int | None:
    """DexScreener pairCreatedAt is ms; if mis-serialized as Unix seconds, normalize to ms."""
    if t is None:
        return None
    if not isinstance(t, (int, float)):
        return None
    if t > 0 and t < 1e12:
        return round(t * 1000)
    return int(t)
