"""Birdeye search and metadata — port of birdeye.ts."""

from __future__ import annotations

import os

import httpx

BIRDEYE_BASE = "https://public-api.birdeye.so"
BIRDEYE_TIMEOUT = 10


def _api_key() -> str | None:
    return os.environ.get("BIRDEYE_API_KEY") or None


def _birdeye_headers() -> dict[str, str]:
    key = _api_key()
    if not key:
        return {}
    return {"X-API-KEY": key, "x-chain": "solana"}


def has_birdeye_key() -> bool:
    return _api_key() is not None


async def search_birdeye(keyword: str) -> list[dict]:
    """Search Birdeye for tokens matching keyword."""
    if not _api_key():
        return []
    try:
        url = f"{BIRDEYE_BASE}/defi/v3/search"
        params = {"chain": "solana", "keyword": keyword, "target": "token"}
        async with httpx.AsyncClient(timeout=BIRDEYE_TIMEOUT) as client:
            resp = await client.get(url, params=params, headers=_birdeye_headers())
            data = resp.json()
        return (data.get("data") or {}).get("items") or []
    except Exception:
        return []


async def get_birdeye_metadata_single(mint: str) -> list[str]:
    """Fetch metadata for a single mint. Returns list of social/website URLs."""
    if not _api_key():
        return []
    try:
        url = f"{BIRDEYE_BASE}/defi/v3/token/meta-data/single"
        params = {"address": mint}
        async with httpx.AsyncClient(timeout=BIRDEYE_TIMEOUT) as client:
            resp = await client.get(url, params=params, headers=_birdeye_headers())
            data = resp.json()
        ext = (data.get("data") or {}).get("extensions") or {}
        urls = []
        for key in ("website", "twitter", "discord", "telegram", "medium"):
            if ext.get(key):
                urls.append(ext[key])
        return urls
    except Exception:
        return []


async def get_birdeye_metadata_multiple(mints: list[str]) -> dict[str, list[str]]:
    """Fetch metadata for up to 50 mints. Returns map of mint → URLs."""
    result: dict[str, list[str]] = {}
    if not _api_key() or not mints:
        return result
    chunk = mints[:50]
    try:
        url = f"{BIRDEYE_BASE}/defi/v3/token/meta-data/multiple"
        params = {"list_address": ",".join(chunk)}
        async with httpx.AsyncClient(timeout=BIRDEYE_TIMEOUT) as client:
            resp = await client.get(url, params=params, headers=_birdeye_headers())
            data = resp.json()
        if data.get("success") and data.get("data"):
            for mint_addr, meta in data["data"].items():
                ext = (meta or {}).get("extensions") or {}
                urls = []
                for key in ("website", "twitter", "discord", "telegram", "medium"):
                    if ext.get(key):
                        urls.append(ext[key])
                if urls:
                    result[mint_addr] = urls
    except Exception:
        pass
    return result


async def get_birdeye_new_listings() -> list[str]:
    """Fetch recently listed tokens from Birdeye. Returns mint addresses."""
    if not _api_key():
        return []
    try:
        url = f"{BIRDEYE_BASE}/defi/v2/tokens/new_listing"
        params = {"limit": "50"}
        async with httpx.AsyncClient(timeout=BIRDEYE_TIMEOUT) as client:
            resp = await client.get(url, params=params, headers=_birdeye_headers())
            data = resp.json()
        items = (data.get("data") or {}).get("items") or []
        return [t.get("address") for t in items if t.get("address")]
    except Exception:
        return []
