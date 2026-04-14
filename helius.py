"""Helius DAS + standard RPC calls — port of helius.ts."""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

from models import HeliusSlotData, HELIUS_TIMEOUT, MAX_SIG_PAGES

PUBLIC_MAINNET_RPC = "https://api.mainnet-beta.solana.com"

# Simple TTL cache for helius slot data
_helius_cache: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 3600


def _get_cached_slot(mint: str) -> dict | None:
    entry = _helius_cache.get(mint)
    if entry and time.time() - entry[0] < _CACHE_TTL:
        return entry[1]
    return None


def _set_cached_slot(mint: str, data: dict) -> None:
    _helius_cache[mint] = (time.time(), data)


def _get_helius_das_rpc_url() -> str | None:
    raw = (os.environ.get("HELIUS_API_KEY") or "").strip()
    if not raw:
        return None
    # If the user pasted the full RPC URL, extract just the key
    if raw.startswith("http"):
        from urllib.parse import urlparse, parse_qs
        try:
            parsed = urlparse(raw)
            key = parse_qs(parsed.query).get("api-key", [None])[0]
            if key:
                return f"https://mainnet.helius-rpc.com/?api-key={key}"
        except Exception:
            pass
        return raw  # already a full URL, use as-is
    return f"https://mainnet.helius-rpc.com/?api-key={raw}"


def _get_standard_json_rpc_url() -> str:
    return _get_helius_das_rpc_url() or (os.environ.get("SOLANA_RPC_URL") or "").strip() or PUBLIC_MAINNET_RPC


async def _json_rpc(url: str, method: str, params: Any) -> Any:
    body = {
        "jsonrpc": "2.0",
        "id": "ogfinder",
        "method": method,
        "params": params,
    }
    async with httpx.AsyncClient(timeout=HELIUS_TIMEOUT) as client:
        resp = await client.post(url, json=body, headers={"Content-Type": "application/json"})
        return resp.json()


async def _standard_rpc(method: str, params: Any) -> Any:
    return await _json_rpc(_get_standard_json_rpc_url(), method, params)


SPL_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
SPL_TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


async def get_asset_batch(mints: list[str]) -> dict[str, HeliusSlotData]:
    """Fetch Helius DAS asset data for a batch of mints."""
    result: dict[str, HeliusSlotData] = {}

    uncached: list[str] = []
    for mint in mints:
        cached = _get_cached_slot(mint)
        if cached:
            result[mint] = HeliusSlotData(slot=cached.get("slot"))
        else:
            uncached.append(mint)

    if not uncached:
        return result

    das_url = _get_helius_das_rpc_url()
    if not das_url:
        return result

    try:
        response = await _json_rpc(das_url, "getAssetBatch", {"ids": uncached})
        assets = response.get("result") if isinstance(response, dict) else None
        if not isinstance(assets, list):
            return result

        for asset in assets:
            if not isinstance(asset, dict) or not asset.get("id"):
                continue
            token_info = asset.get("token_info") or {}
            supply_info = asset.get("supply") or {}
            supply = token_info.get("supply") or supply_info.get("print_current_supply")

            content = asset.get("content") or {}
            metadata = content.get("metadata") or {}

            data = HeliusSlotData(
                slot=asset.get("slot"),
                created_at=asset.get("created_at"),
                helius_name=metadata.get("name"),
                helius_symbol=metadata.get("symbol"),
                token_interface=asset.get("interface"),
                supply=supply,
            )
            result[asset["id"]] = data
            if data.slot is not None:
                _set_cached_slot(asset["id"], {"slot": data.slot, "blockTime": 0})
    except Exception:
        pass

    return result


def _parse_mint_from_account_response(response: Any) -> HeliusSlotData | None:
    """Parse a mint from getAccountInfo(jsonParsed) response."""
    if not isinstance(response, dict):
        return None
    if response.get("error"):
        return None
    result = response.get("result")
    if not isinstance(result, dict):
        return None
    value = result.get("value")
    if not isinstance(value, dict):
        return None
    owner = value.get("owner")
    if owner not in (SPL_TOKEN_PROGRAM, SPL_TOKEN_2022_PROGRAM):
        return None
    data = value.get("data")
    if not isinstance(data, dict):
        return None
    parsed = data.get("parsed")
    if not isinstance(parsed, dict) or parsed.get("type") != "mint":
        return None
    info = parsed.get("info") or {}
    supply_str = info.get("supply")
    supply = None
    if supply_str is not None and supply_str != "":
        try:
            supply = float(supply_str)
        except (ValueError, TypeError):
            pass

    return HeliusSlotData(
        token_interface="FungibleToken",
        supply=supply,
    )


async def get_mint_helius_data_rpc_fallback(mint: str) -> HeliusSlotData | None:
    """Fallback when DAS has no record: use standard RPC getAccountInfo."""
    params = [mint, {"encoding": "jsonParsed"}]
    urls = []
    standard = _get_standard_json_rpc_url()
    urls.append(standard)
    if PUBLIC_MAINNET_RPC != standard:
        urls.append(PUBLIC_MAINNET_RPC)
    rpc_env = (os.environ.get("SOLANA_RPC_URL") or "").strip()
    if rpc_env and rpc_env not in urls:
        urls.append(rpc_env)

    for url in urls:
        try:
            response = await _json_rpc(url, "getAccountInfo", params)
            parsed = _parse_mint_from_account_response(response)
            if parsed:
                return parsed
        except Exception:
            continue
    return None


async def get_creation_slot(mint: str) -> dict | None:
    """Get actual creation slot/blockTime by paginating getSignaturesForAddress."""
    cached = _get_cached_slot(mint)
    if cached and cached.get("blockTime", 0) > 0:
        return cached

    try:
        before: str | None = None
        oldest_sig: dict | None = None

        for _ in range(MAX_SIG_PAGES):
            params: list[Any] = [mint, {"limit": 1000}]
            if before:
                params[1]["before"] = before

            response = await _standard_rpc("getSignaturesForAddress", params)
            sigs = response.get("result") if isinstance(response, dict) else None
            if not isinstance(sigs, list) or len(sigs) == 0:
                break

            oldest_sig = sigs[-1]
            before = oldest_sig.get("signature")
            if len(sigs) < 1000:
                break

        if not oldest_sig:
            return None

        data = {"slot": oldest_sig.get("slot"), "blockTime": oldest_sig.get("blockTime", 0)}
        _set_cached_slot(mint, data)
        return data
    except Exception:
        return None
