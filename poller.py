"""Background poller for DexScreener profiles/boosts — port of poller.ts."""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from models import DEX_TIMEOUT
from url_index import upsert_token_links, count_indexed_tokens
from birdeye import get_birdeye_new_listings, get_birdeye_metadata_multiple, has_birdeye_key

logger = logging.getLogger("ogfinder")

TOKEN_PROFILES_LATEST = "https://api.dexscreener.com/token-profiles/latest/v1"
TOKEN_BOOSTS_LATEST = "https://api.dexscreener.com/token-boosts/latest/v1"

POLL_INTERVAL = 120  # seconds

_poller_started = False


async def _poll_dex_profiles() -> int:
    """Fetch latest DexScreener profiles/boosts and index their social links."""
    indexed = 0
    for endpoint in (TOKEN_PROFILES_LATEST, TOKEN_BOOSTS_LATEST):
        try:
            async with httpx.AsyncClient(timeout=DEX_TIMEOUT) as client:
                resp = await client.get(endpoint)
                data = resp.json()
            if not isinstance(data, list):
                continue
            for tp in data:
                if not isinstance(tp, dict) or tp.get("chainId") != "solana":
                    continue
                token_address = tp.get("tokenAddress")
                if not token_address:
                    continue
                urls = []
                for link in tp.get("links") or []:
                    if isinstance(link, dict) and link.get("url"):
                        urls.append(link["url"])
                if urls:
                    try:
                        upsert_token_links(token_address, urls, "dexscreener-poll")
                        indexed += 1
                    except Exception:
                        pass
        except Exception:
            continue
    return indexed


async def _poll_birdeye_new_listings() -> int:
    """Index social metadata for recently listed tokens from Birdeye."""
    if not has_birdeye_key():
        return 0
    try:
        mints = await get_birdeye_new_listings()
        if not mints:
            return 0
        meta_map = await get_birdeye_metadata_multiple(mints)
        indexed = 0
        for mint, urls in meta_map.items():
            if urls:
                try:
                    upsert_token_links(mint, urls, "birdeye-poll")
                    indexed += 1
                except Exception:
                    pass
        return indexed
    except Exception:
        return 0


async def _poll_loop() -> None:
    """Main polling loop — runs forever."""
    while True:
        try:
            dex_count = await _poll_dex_profiles()
            birdeye_count = await _poll_birdeye_new_listings()
            total = count_indexed_tokens()
            logger.info(f"[poller] indexed dex={dex_count} birdeye={birdeye_count} total={total}")
        except Exception as e:
            logger.warning(f"[poller] error: {e}")
        await asyncio.sleep(POLL_INTERVAL)


def ensure_poller_started() -> None:
    """Start the background poller if not already started."""
    global _poller_started
    if _poller_started:
        return
    _poller_started = True
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_poll_loop())
    except RuntimeError:
        # No running loop yet — will be started when the bot starts
        asyncio.ensure_future(_poll_loop())
