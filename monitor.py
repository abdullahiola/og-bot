"""Trending token monitor — polls DexScreener top boosts, finds OGs, sends alerts."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import httpx

from models import DEX_TIMEOUT, MIN_QUERY
from normalize import normalize
from search import search_tokens
from enrich import build_token_results

logger = logging.getLogger("ogfinder")

TOP_BOOSTS_URL = "https://api.dexscreener.com/token-boosts/top/v1"
MONITOR_INTERVAL = 180  # check every 3 minutes
ALERT_COOLDOWN = 3600   # don't re-alert the same token name within 1 hour
TOP_N = 10              # only look at the top N boosted tokens


@dataclass
class TrendingToken:
    mint: str
    name: str
    boost_amount: int


# ── State ─────────────────────────────────────────────────────────────

# {user_id: True} — users who have monitoring enabled
_monitor_subscribers: dict[int, bool] = {}

# {normalized_name: timestamp} — cooldown to avoid spamming same token
_alerted: dict[str, float] = {}

_monitor_task: asyncio.Task | None = None
_bot_app = None  # set by start_monitor()


def is_monitoring(user_id: int) -> bool:
    return _monitor_subscribers.get(user_id, False)


def toggle_monitor(user_id: int) -> bool:
    """Toggle monitoring for a user. Returns the new state."""
    current = _monitor_subscribers.get(user_id, False)
    _monitor_subscribers[user_id] = not current
    return not current


def get_subscriber_ids() -> list[int]:
    return [uid for uid, active in _monitor_subscribers.items() if active]


# ── Fetch trending ────────────────────────────────────────────────────

async def _fetch_top_trending() -> list[TrendingToken]:
    """Fetch the top boosted Solana tokens from DexScreener."""
    try:
        async with httpx.AsyncClient(timeout=DEX_TIMEOUT) as client:
            resp = await client.get(TOP_BOOSTS_URL)
            data = resp.json()
    except Exception as e:
        logger.warning("[monitor] Failed to fetch trending: %s", e)
        return []

    if not isinstance(data, list):
        return []

    tokens: list[TrendingToken] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        if item.get("chainId") != "solana":
            continue
        mint = item.get("tokenAddress", "")
        desc = (item.get("description") or "").strip()
        boost = item.get("totalAmount", 0)
        if not mint:
            continue
        # Use description as name; first line only, truncated
        name = desc.split("\n")[0].strip() if desc else ""
        if not name:
            # Try to extract from URL slug
            url = item.get("url", "")
            name = url.rstrip("/").split("/")[-1] if url else ""
        if name and len(name) >= MIN_QUERY:
            tokens.append(TrendingToken(mint=mint, name=name, boost_amount=boost))

    # Sort by boost amount descending, take top N
    tokens.sort(key=lambda t: t.boost_amount, reverse=True)
    return tokens[:TOP_N]


# ── OG search for a trending token ───────────────────────────────────

async def _find_og_for_token(token: TrendingToken) -> dict | None:
    """Search for the OG of a trending token. Returns info dict or None."""
    search_term = token.name[:30]
    try:
        raw_tokens = await search_tokens(search_term)
        if not raw_tokens:
            return None

        results = await build_token_results(raw_tokens, search_term)
        if not results:
            return None

        og = results[0]  # rank 1 = the OG
        return {
            "trending_name": token.name,
            "trending_mint": token.mint,
            "trending_boost": token.boost_amount,
            "og_name": og.display_name,
            "og_symbol": og.display_symbol,
            "og_mint": og.mint,
            "og_created_at": og.created_at or "unknown",
            "og_confidence": og.confidence,
            "og_confidence_label": og.confidence_label,
            "is_same": og.mint == token.mint,
            "total_found": len(results),
        }
    except Exception as e:
        logger.warning("[monitor] OG search failed for %s: %s", token.name, e)
        return None


# ── Format alert message ─────────────────────────────────────────────

def _format_boost_bar(amount: int) -> str:
    """Visual bar representing boost strength."""
    if amount >= 500:
        return "🟢🟢🟢🟢🟢"
    if amount >= 300:
        return "🟢🟢🟢🟢⚫"
    if amount >= 150:
        return "🟢🟢🟢⚫⚫"
    if amount >= 80:
        return "🟢🟢⚫⚫⚫"
    return "🟢⚫⚫⚫⚫"


def _format_alert(info: dict) -> str:
    """Format a trending OG alert message."""
    mint = info["og_mint"]
    lines = []

    # ── Header
    lines.append("🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥")
    lines.append("")
    lines.append("📡 <b>TRENDING ALERT</b>")
    lines.append("")

    # ── Trending token info
    lines.append(f"<b>{_escape(info['trending_name'])}</b> is heating up on DexScreener")
    boost_bar = _format_boost_bar(info["trending_boost"])
    lines.append(f"{boost_bar}  <b>{info['trending_boost']:,}</b> boosts")
    lines.append("")

    # ── Separator
    lines.append("▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬")
    lines.append("")

    # ── OG result
    if info["is_same"]:
        lines.append("✅ <b>THIS IS THE OG</b>")
        lines.append(f"   <b>{_escape(info['og_name'])}</b> (${_escape(info['og_symbol'])})")
    else:
        lines.append("🏆 <b>THE OG</b>")
        lines.append(f"   <b>{_escape(info['og_name'])}</b> · ${_escape(info['og_symbol'])}")

        # Date
        date_str = info["og_created_at"]
        if date_str and date_str != "unknown":
            lines.append(f"   📅 {date_str[:16]}")

        # Confidence
        conf = info["og_confidence"]
        filled = min(conf, 5)
        empty = 5 - filled
        stars = "★" * filled + "☆" * empty
        lines.append(f"   {stars}  {info['og_confidence_label']}")

    lines.append("")

    # ── CA block (prominent)
    lines.append("┌─────────────────────────┐")
    lines.append(f"  <b>OG Contract Address</b>")
    lines.append(f"  <code>{mint}</code>")
    lines.append("└─────────────────────────┘")
    lines.append("")

    # ── Quick links row
    dex_link = f'<a href="https://dexscreener.com/solana/{mint}">DexScreener</a>'
    sol_link = f'<a href="https://solscan.io/token/{mint}">Solscan</a>'
    bird_link = f'<a href="https://birdeye.so/token/{mint}?chain=solana">Birdeye</a>'
    lines.append(f"🔗 {dex_link} · {sol_link} · {bird_link}")
    lines.append(f"📊 {info['total_found']} tokens found with this name")

    return "\n".join(lines)


def _escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ── Monitor loop ─────────────────────────────────────────────────────

async def _monitor_loop() -> None:
    """Background loop: fetch trending, find OGs, alert subscribers."""
    # Wait a bit before first run to let the bot fully initialize
    await asyncio.sleep(10)

    while True:
        try:
            subscribers = get_subscriber_ids()
            if not subscribers:
                await asyncio.sleep(MONITOR_INTERVAL)
                continue

            trending = await _fetch_top_trending()
            now = time.time()

            for token in trending:
                norm = normalize(token.name)

                # Skip if we already alerted this name recently
                last_alert = _alerted.get(norm, 0)
                if now - last_alert < ALERT_COOLDOWN:
                    continue

                info = await _find_og_for_token(token)
                if not info:
                    continue

                _alerted[norm] = now
                msg = _format_alert(info)

                # Send to all subscribers
                for uid in subscribers:
                    try:
                        await _bot_app.bot.send_message(
                            chat_id=uid,
                            text=msg,
                            parse_mode="HTML",
                            disable_web_page_preview=True,
                        )
                    except Exception as e:
                        logger.warning("[monitor] Failed to send alert to %s: %s", uid, e)

                # Small delay between tokens to avoid rate limits
                await asyncio.sleep(1)

            # Clean up old cooldown entries
            cutoff = now - ALERT_COOLDOWN * 2
            _alerted_copy = {k: v for k, v in _alerted.items() if v > cutoff}
            _alerted.clear()
            _alerted.update(_alerted_copy)

        except Exception as e:
            logger.error("[monitor] Error in monitor loop: %s", e, exc_info=True)

        await asyncio.sleep(MONITOR_INTERVAL)


def start_monitor(app) -> None:
    """Start the monitor background task. Call from post_init."""
    global _monitor_task, _bot_app
    if _monitor_task is not None:
        return
    _bot_app = app
    _monitor_task = asyncio.get_running_loop().create_task(_monitor_loop())
    logger.info("[monitor] Trending monitor started (interval=%ds)", MONITOR_INTERVAL)
