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

# {chat_id: True} — chats (private or group) with monitoring enabled
_monitor_subscribers: dict[int, bool] = {}

# {normalized_name: timestamp} — cooldown to avoid spamming same token
_alerted: dict[str, float] = {}

_monitor_task: asyncio.Task | None = None
_bot_app = None  # set by start_monitor()


def is_monitoring(chat_id: int) -> bool:
    return _monitor_subscribers.get(chat_id, False)


def toggle_monitor(chat_id: int) -> bool:
    """Toggle monitoring for a chat. Returns the new state."""
    current = _monitor_subscribers.get(chat_id, False)
    _monitor_subscribers[chat_id] = not current
    return not current


def get_subscriber_chat_ids() -> list[int]:
    return [cid for cid, active in _monitor_subscribers.items() if active]


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


async def _fetch_token_market_data(mint: str) -> dict:
    """Fetch market cap and symbol for a token from DexScreener."""
    try:
        url = f"https://api.dexscreener.com/tokens/v1/solana/{mint}"
        async with httpx.AsyncClient(timeout=DEX_TIMEOUT) as client:
            resp = await client.get(url)
            data = resp.json()
        if isinstance(data, list) and data:
            pair = data[0]
            base = pair.get("baseToken", {})
            return {
                "symbol": base.get("symbol", "???"),
                "name": base.get("name", ""),
                "mcap": pair.get("marketCap"),
                "fdv": pair.get("fdv"),
                "price_usd": pair.get("priceUsd"),
            }
    except Exception:
        pass
    return {}


# ── OG search for a trending token ───────────────────────────────────

async def _find_og_for_token(token: TrendingToken) -> dict | None:
    """Search for the OG of a trending token. Returns info dict or None."""
    search_term = token.name[:30]
    try:
        # Fetch trending token market data and OG results in parallel
        raw_tokens_task = search_tokens(search_term)
        market_task = _fetch_token_market_data(token.mint)

        raw_tokens, market = await asyncio.gather(raw_tokens_task, market_task)

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
            "trending_symbol": market.get("symbol", "???"),
            "trending_mcap": market.get("mcap"),
            "trending_fdv": market.get("fdv"),
            "og_name": og.display_name,
            "og_symbol": og.display_symbol,
            "og_mint": og.mint,
            "og_created_at": og.created_at or "unknown",
            "og_confidence": og.confidence,
            "og_confidence_label": og.confidence_label,
            "og_mcap": og.market_cap_usd or og.fdv_usd,
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


def _format_mcap(val) -> str:
    """Format market cap value."""
    if val is None:
        return "—"
    try:
        val = float(val)
    except (TypeError, ValueError):
        return "—"
    if val >= 1_000_000_000:
        return f"${val / 1_000_000_000:.2f}B"
    if val >= 1_000_000:
        return f"${val / 1_000_000:.2f}M"
    if val >= 1_000:
        return f"${val / 1_000:.1f}K"
    return f"${val:.0f}"


def _format_alert(info: dict) -> str:
    """Format a trending OG alert message."""
    t_mint = info["trending_mint"]
    og_mint = info["og_mint"]
    lines = []

    # ── Header
    lines.append("<b>TRENDING ALERT</b>")
    lines.append("")

    # ── Trending token
    lines.append(f"<b>{_escape(info['trending_name'])}</b> · ${_escape(info['trending_symbol'])}")
    mcap_str = _format_mcap(info.get("trending_mcap") or info.get("trending_fdv"))
    lines.append(f"MCap: <b>{mcap_str}</b> · {info['trending_boost']:,} boosts")
    lines.append(f"<code>{t_mint}</code>")

    lines.append("")
    lines.append("———")
    lines.append("")

    # ── OG result
    if info["is_same"]:
        lines.append("<b>This is the OG.</b>")
    else:
        lines.append(f"<b>OG: {_escape(info['og_name'])}</b> · ${_escape(info['og_symbol'])}")

        date_str = info["og_created_at"]
        if date_str and date_str != "unknown":
            lines.append(f"Created: {date_str[:16]}")

        og_mcap = _format_mcap(info.get("og_mcap"))
        if og_mcap != "—":
            lines.append(f"MCap: <b>{og_mcap}</b>")

        conf = info["og_confidence"]
        filled = min(conf, 5)
        empty = 5 - filled
        stars = "★" * filled + "☆" * empty
        lines.append(f"{stars} {info['og_confidence_label']}")

        lines.append("")
        lines.append(f"<b>OG CA:</b>")
        lines.append(f"<code>{og_mint}</code>")

    lines.append("")

    # ── Links
    target = og_mint
    dex_link = f'<a href="https://dexscreener.com/solana/{target}">DexScreener</a>'
    sol_link = f'<a href="https://solscan.io/token/{target}">Solscan</a>'
    bird_link = f'<a href="https://birdeye.so/token/{target}?chain=solana">Birdeye</a>'
    lines.append(f"{dex_link} · {sol_link} · {bird_link}")
    lines.append(f"{info['total_found']} tokens with this name")

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
            subscribers = get_subscriber_chat_ids()
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

                # Send to all subscribed chats (private + groups)
                for cid in subscribers:
                    try:
                        await _bot_app.bot.send_message(
                            chat_id=cid,
                            text=msg,
                            parse_mode="HTML",
                            disable_web_page_preview=True,
                        )
                    except Exception as e:
                        logger.warning("[monitor] Failed to send alert to %s: %s", cid, e)

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
