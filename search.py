"""Merged token search: DexScreener + Jupiter — port of search.ts."""

from __future__ import annotations

import asyncio

from models import RawToken, MAX_HELIUS
from dex import search_dex
from jupiter import search_jupiter


async def search_tokens(query: str) -> list[RawToken]:
    dex_results, jup_results = await asyncio.gather(
        search_dex(query),
        search_jupiter(query),
    )

    merged: dict[str, RawToken] = {}

    for token in dex_results:
        merged[token.mint] = token

    for token in jup_results:
        existing = merged.get(token.mint)
        if existing:
            existing.jup_name = token.jup_name
            existing.jup_symbol = token.jup_symbol
        else:
            merged[token.mint] = token

    deduped = list(merged.values())
    return deduped[:MAX_HELIUS]
