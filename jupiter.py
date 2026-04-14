"""Jupiter token list search — port of jupiter.ts."""

from __future__ import annotations

import time

import httpx

from models import RawToken, JUP_LIMIT, CACHE_JUP
from normalize import normalize

JUP_URL = "https://tokens.jup.ag/tokens"

_jupiter_tokens: list[dict] | None = None
_jupiter_loaded_at: float = 0
_jupiter_by_mint: dict[str, dict] | None = None


async def _get_jupiter_list() -> list[dict]:
    global _jupiter_tokens, _jupiter_loaded_at, _jupiter_by_mint
    if _jupiter_tokens is not None and time.time() - _jupiter_loaded_at < CACHE_JUP:
        return _jupiter_tokens

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(JUP_URL)
            data = resp.json()
        if isinstance(data, list):
            _jupiter_tokens = data
            _jupiter_by_mint = None
            _jupiter_loaded_at = time.time()
            return _jupiter_tokens
        return _jupiter_tokens or []
    except Exception:
        return _jupiter_tokens or []


async def search_jupiter(query: str) -> list[RawToken]:
    normalized_query = normalize(query)
    token_list = await _get_jupiter_list()

    results: list[RawToken] = []
    for token in token_list:
        name = normalize(token.get("name", ""))
        symbol = normalize(token.get("symbol", ""))
        if normalized_query in name or normalized_query in symbol:
            results.append(RawToken(
                mint=token.get("address", ""),
                jup_name=token.get("name"),
                jup_symbol=token.get("symbol"),
            ))
            if len(results) >= JUP_LIMIT:
                break

    return results


async def get_jupiter_token_by_mint(mint: str) -> dict | None:
    """O(1) lookup after list load — used when DAS has no metadata for a mint."""
    global _jupiter_by_mint
    token_list = await _get_jupiter_list()
    if not token_list:
        return None
    if _jupiter_by_mint is None:
        _jupiter_by_mint = {t["address"]: t for t in token_list if "address" in t}
    t = _jupiter_by_mint.get(mint)
    if not t:
        return None
    return {"name": t.get("name", ""), "symbol": t.get("symbol", "")}
