"""DexScreener search API integration — port of dex.ts."""

from __future__ import annotations

import httpx

from models import RawToken, DEX_LIMIT, DEX_TIMEOUT
from normalize import normalize, dex_pair_created_ms

DEX_URL = "https://api.dexscreener.com/latest/dex/search"

# Simple TTL cache
_dex_cache: dict[str, tuple[float, list[RawToken]]] = {}
_CACHE_TTL = 300  # seconds


def _get_cached(key: str) -> list[RawToken] | None:
    import time
    entry = _dex_cache.get(key)
    if entry and time.time() - entry[0] < _CACHE_TTL:
        return entry[1]
    return None


def _set_cached(key: str, value: list[RawToken]) -> None:
    import time
    _dex_cache[key] = (time.time(), value)


async def search_dex(query: str) -> list[RawToken]:
    normalized_query = normalize(query)
    cached = _get_cached(normalized_query)
    if cached is not None:
        return cached

    try:
        async with httpx.AsyncClient(timeout=DEX_TIMEOUT) as client:
            resp = await client.get(DEX_URL, params={"q": query})
            data = resp.json()
    except Exception:
        return []

    pairs = data.get("pairs") or []

    # Re-filter: DexScreener search is fuzzy. Require normalized name/symbol to contain query.
    solana_pairs = []
    for p in pairs:
        if p.get("chainId") != "solana":
            continue
        base = p.get("baseToken", {})
        name = normalize(base.get("name", ""))
        symbol = normalize(base.get("symbol", ""))
        if normalized_query in name or normalized_query in symbol:
            solana_pairs.append(p)

    # Group by baseToken.address, keep oldest pairCreatedAt per token
    token_map: dict[str, tuple[dict, int | None]] = {}
    for pair in solana_pairs:
        mint = pair.get("baseToken", {}).get("address", "")
        if not mint:
            continue
        pair_ms = dex_pair_created_ms(pair.get("pairCreatedAt"))
        existing = token_map.get(mint)
        if existing is None:
            token_map[mint] = (pair, pair_ms)
        elif pair_ms is not None and (existing[1] is None or pair_ms < existing[1]):
            token_map[mint] = (existing[0], pair_ms)

    tokens: list[RawToken] = []
    for mint, (pair, oldest_time) in token_map.items():
        base = pair.get("baseToken", {})
        tokens.append(RawToken(
            mint=mint,
            dex_name=base.get("name"),
            dex_symbol=base.get("symbol"),
            dex_id=pair.get("dexId"),
            pair_created_at=oldest_time,
        ))
        if len(tokens) >= DEX_LIMIT:
            break

    _set_cached(normalized_query, tokens)
    return tokens
