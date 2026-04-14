"""OGfinder Telegram Bot — entry point."""

from __future__ import annotations

import logging
import os
import time

from dotenv import load_dotenv
from telegram import Update, ChatMemberUpdated
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ChatMemberHandler,
    filters, ContextTypes,
)

from models import MIN_QUERY, MAX_QUERY, MAX_MINT_LEN, MAX_SOCIAL_URL, SearchResponse
from normalize import normalize
from solana_utils import is_likely_mint_address
from social_url import is_likely_social_url
from search import search_tokens
from dex_social import search_dex_by_social_url
from enrich import build_token_results
from helius import get_asset_batch, get_mint_helius_data_rpc_fallback
from jupiter import get_jupiter_token_by_mint
from formatting import format_search_results
from poller import ensure_poller_started
from stats import track_user, track_group_join, track_group_left, track_group_activity, log_search, get_stats, format_subscriber_count, get_active_group_ids
from monitor import toggle_monitor, is_monitoring, start_monitor

load_dotenv()

logging.basicConfig(
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("ogfinder")


# ── Helpers ────────────────────────────────────────────────────────────

async def _fetch_dex_token_meta(mint: str) -> dict | None:
    """Fetch token name/symbol from DexScreener tokens/v1 as a last resort."""
    import httpx
    try:
        url = f"https://api.dexscreener.com/tokens/v1/solana/{mint}"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            data = resp.json()
        if isinstance(data, list):
            for pair in data:
                base = pair.get("baseToken") or {}
                name = base.get("name", "")
                symbol = base.get("symbol", "")
                if name and len(name) >= 2:
                    return {"name": name, "symbol": symbol}
    except Exception:
        pass
    return None


# ── Search logic (mirrors route.ts) ───────────────────────────────────

async def _do_search(query: str) -> SearchResponse:
    """Execute the appropriate search mode and return a SearchResponse."""
    start = time.time()
    q = query.strip()

    # Social URL search
    if is_likely_social_url(q):
        if len(q) < 8 or len(q) > MAX_SOCIAL_URL:
            return SearchResponse(error=f"Social link must be 8–{MAX_SOCIAL_URL} characters")
        raw_tokens = await search_dex_by_social_url(q)
        if not raw_tokens:
            return SearchResponse(
                query=normalize(q), mode="social",
                timing=(time.time() - start) * 1000,
            )
        final = await build_token_results(raw_tokens, q, rank_by="marketcap")
        elapsed = (time.time() - start) * 1000
        return SearchResponse(
            results=final, query=normalize(q), total_found=len(final),
            timing=elapsed, mode="social",
        )

    # Mint scan
    if is_likely_mint_address(q):
        if len(q) < 32 or len(q) > MAX_MINT_LEN:
            return SearchResponse(error=f"Mint must be 32–{MAX_MINT_LEN} base58 characters")

        pre = await get_asset_batch([q])
        h = pre.get(q)

        if not h:
            fb = await get_mint_helius_data_rpc_fallback(q)
            if fb:
                jup = await get_jupiter_token_by_mint(q)
                if jup:
                    fb.helius_name = jup["name"]
                    fb.helius_symbol = jup["symbol"]
                h = fb

        if not h:
            return SearchResponse(error="Token not found on-chain")

        if h.token_interface and h.token_interface not in ("FungibleToken", "FungibleAsset"):
            return SearchResponse(error="Not a fungible token (NFT or unsupported type)")

        # Derive search term
        search_term = _derive_search_term(h.helius_name, h.helius_symbol)
        if len(search_term) < MIN_QUERY:
            jup = await get_jupiter_token_by_mint(q)
            if jup:
                h.helius_name = jup["name"]
                h.helius_symbol = jup["symbol"]
                search_term = _derive_search_term(h.helius_name, h.helius_symbol)

        # DexScreener fallback for pump.fun / unindexed tokens
        if len(search_term) < MIN_QUERY:
            dex_meta = await _fetch_dex_token_meta(q)
            if dex_meta:
                h.helius_name = dex_meta["name"]
                h.helius_symbol = dex_meta["symbol"]
                search_term = _derive_search_term(h.helius_name, h.helius_symbol)

        if len(search_term) < MIN_QUERY:
            return SearchResponse(error="This mint has no name/symbol long enough to search for duplicates")

        raw_tokens = await search_tokens(search_term)
        if not any(t.mint == q for t in raw_tokens):
            from models import RawToken
            raw_tokens.append(RawToken(
                mint=q,
                jup_name=h.helius_name,
                jup_symbol=h.helius_symbol,
            ))

        final = await build_token_results(raw_tokens, search_term, scanned_mint=q)
        scanned = next((t for t in final if t.mint == q), None)
        elapsed = (time.time() - start) * 1000

        return SearchResponse(
            results=final, query=normalize(search_term), total_found=len(final),
            timing=elapsed, mode="scan", scanned_mint=q,
            scan_name=h.helius_name, scan_symbol=h.helius_symbol,
            is_scanned_og=scanned.rank == 1 if scanned else False,
            scanned_rank=scanned.rank if scanned else None,
        )

    # Text search
    if len(q) < MIN_QUERY or len(q) > MAX_QUERY:
        return SearchResponse(error=f"Search must be {MIN_QUERY}–{MAX_QUERY} characters")

    raw_tokens = await search_tokens(q)
    if not raw_tokens:
        return SearchResponse(
            query=normalize(q), mode="search",
            timing=(time.time() - start) * 1000,
        )

    final = await build_token_results(raw_tokens, q)
    elapsed = (time.time() - start) * 1000
    return SearchResponse(
        results=final, query=normalize(q), total_found=len(final),
        timing=elapsed, mode="search",
    )


def _derive_search_term(name: str | None, symbol: str | None) -> str:
    n = (name or "").strip()
    s = (symbol or "").strip()
    if len(n) >= MIN_QUERY:
        return n[:MAX_QUERY]
    if len(s) >= MIN_QUERY:
        return s[:MAX_QUERY]
    if n:
        return n[:MAX_QUERY]
    if s:
        return s[:MAX_QUERY]
    return ""


# ── Telegram handlers ─────────────────────────────────────────────────

def _track_interaction(update: Update) -> None:
    """Track user and group on every interaction."""
    user = update.effective_user
    chat = update.effective_chat
    if user:
        track_user(user.id, user.username, user.first_name)
    if chat and chat.type in ("group", "supergroup"):
        track_group_activity(chat.id, chat.title)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _track_interaction(update)
    text = (
        "👋 Hey! Welcome to <b>OGfinder Bot</b>\n\n"
        "I find the original Solana token so you never buy a copycat.\n\n"
        "<b>Commands</b>\n"
        "🔍 /og &lt;name&gt; — search by name\n"
        "📋 /findog &lt;ca&gt; — scan a contract address\n"
        "🔗 /link &lt;url&gt; — search by social link\n"
        "📡 /monitor — toggle trending alerts\n\n"
        "Or just send any text in DM and I'll auto-detect."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_og(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _track_interaction(update)
    query = " ".join(context.args) if context.args else ""
    if not query:
        await update.message.reply_text("Usage: /og <token name>\nExample: /og pepe")
        return
    await _handle_query(update, query)


async def cmd_findog(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _track_interaction(update)
    query = " ".join(context.args) if context.args else ""
    if not query:
        await update.message.reply_text("Usage: /findog <ca>\nPaste a Solana contract address to check if it's the OG.")
        return
    await _handle_query(update, query)


async def cmd_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _track_interaction(update)
    query = " ".join(context.args) if context.args else ""
    if not query:
        await update.message.reply_text("Usage: /link <url>\nExample: /link https://x.com/someproject")
        return
    await _handle_query(update, query)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display bot usage statistics (admin only)."""
    _track_interaction(update)
    admin_id = os.environ.get("ADMIN_USER_ID", "").strip()
    user = update.effective_user
    if not admin_id or not user or str(user.id) != admin_id:
        await update.message.reply_text("⛔ This command is restricted to the bot admin.")
        return
    s = get_stats()
    text = (
        "<b>OGfinder Stats</b>\n\n"
        f"Users: <b>{s['total_users']:,}</b>\n"
        f"Active (24h): <b>{s['active_24h']:,}</b>\n"
        f"Groups: <b>{s['active_groups']:,}</b>\n\n"
        f"Total searches: <b>{s['total_searches']:,}</b>\n"
        f"Searches (24h): <b>{s['searches_24h']:,}</b>"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message to all groups the bot is in (admin only)."""
    admin_id = os.environ.get("ADMIN_USER_ID", "").strip()
    user = update.effective_user
    if not admin_id or not user or str(user.id) != admin_id:
        return

    message = " ".join(context.args) if context.args else ""
    if not message:
        await update.message.reply_text("Usage: /broadcast <message>")
        return

    group_ids = get_active_group_ids()
    if not group_ids:
        await update.message.reply_text("No active groups.")
        return

    sent = 0
    failed = 0
    for gid in group_ids:
        try:
            await context.bot.send_message(
                chat_id=gid,
                text=message,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            sent += 1
        except Exception as e:
            logger.warning("Broadcast failed for %s: %s", gid, e)
            failed += 1

    await update.message.reply_text(f"Sent to {sent} groups. Failed: {failed}.")


async def cmd_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle the DexScreener trending monitor for this chat."""
    _track_interaction(update)
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    # In groups, only admins can toggle the monitor
    if chat.type in ("group", "supergroup"):
        member = await chat.get_member(user.id)
        if member.status not in ("administrator", "creator"):
            await update.message.reply_text("⛔ Only group admins can toggle the monitor.")
            return

    new_state = toggle_monitor(chat.id)
    if new_state:
        where = "this group" if chat.type in ("group", "supergroup") else "you"
        text = (
            "<b>Trending monitor ON</b>\n\n"
            f"Alerts will be sent to {where} when tokens trend on DexScreener, "
            "along with the OG token's CA.\n\n"
            "Checking every ~3 minutes.\n"
            "Use /monitor again to turn off."
        )
    else:
        text = "<b>Trending monitor OFF</b>\n\nNo more trending alerts in this chat."

    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Auto-detect search mode from plain text — private chats only."""
    if not update.message or not update.message.text:
        return
    _track_interaction(update)
    # Only auto-detect in private chats; groups require /commands
    if update.message.chat.type != "private":
        return
    query = update.message.text.strip()
    if not query or query.startswith("/"):
        return
    await _handle_query(update, query)


async def handle_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Track when the bot is added to or removed from groups."""
    result: ChatMemberUpdated | None = update.my_chat_member
    if not result:
        return
    chat = result.chat
    old = result.old_chat_member
    new = result.new_chat_member
    if chat.type not in ("group", "supergroup"):
        return
    # Bot was added to the group
    if new.status in ("member", "administrator") and old.status in ("left", "kicked"):
        track_group_join(chat.id, chat.title)
        logger.info("Bot added to group: %s (%s)", chat.title, chat.id)

        # Send welcome message
        welcome = (
            "👋 Hey! <b>OGfinder</b> has entered the chat.\n\n"
            "I find the original Solana token so you never buy a copycat.\n\n"
            "🔍 /og &lt;name&gt; — find the OG token\n"
            "📋 /findog &lt;ca&gt; — scan a contract address\n"
            "🔗 /link &lt;url&gt; — search by social link\n"
            "📡 /monitor — toggle trending alerts"
        )
        try:
            await context.bot.send_message(
                chat_id=chat.id,
                text=welcome,
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.warning("Failed to send welcome message to %s: %s", chat.id, e)

    # Bot was removed from the group
    elif new.status in ("left", "kicked"):
        track_group_left(chat.id)
        logger.info("Bot removed from group: %s (%s)", chat.title, chat.id)


async def _handle_query(update: Update, query: str) -> None:
    """Execute search and send formatted results."""
    # Send "searching..." indicator
    msg = await update.message.reply_text("🔍 Searching…", parse_mode=ParseMode.HTML)

    try:
        resp = await _do_search(query)
    except Exception as e:
        logger.error("Search error: %s", e, exc_info=True)
        await msg.edit_text("❌ Something went wrong. Please try again.")
        return

    if resp.error:
        await msg.edit_text(f"⚠️ {resp.error}")
        return

    # Log the search
    user = update.effective_user
    chat = update.effective_chat
    if user and chat:
        log_search(user.id, chat.id, query, resp.mode)

    # Get subscriber count for footer
    stats = get_stats()
    subscriber_line = f"👥 {format_subscriber_count(stats['total_users'])} users"

    text = format_search_results(
        results=resp.results,
        query=resp.query,
        mode=resp.mode,
        timing=resp.timing,
        total_found=resp.total_found,
        scan_name=resp.scan_name,
        scan_symbol=resp.scan_symbol,
        scanned_mint=resp.scanned_mint,
        is_scanned_og=resp.is_scanned_og,
        scanned_rank=resp.scanned_rank,
        subscriber_line=subscriber_line,
    )

    # Telegram has a 4096 char limit
    if len(text) > 4096:
        text = text[:4090] + "\n…"

    await msg.edit_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


# ── Main ───────────────────────────────────────────────────────────────

def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("❌ TELEGRAM_BOT_TOKEN not set. Create a .env file with your bot token.")
        print("   Get one from @BotFather on Telegram.")
        return

    logger.info("Starting OGfinder bot…")

    app = Application.builder().token(token).build()

    # Register handlers — commands work in both private chats and groups
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("og", cmd_og))
    app.add_handler(CommandHandler("findog", cmd_findog))
    app.add_handler(CommandHandler("link", cmd_link))
    app.add_handler(CommandHandler("monitor", cmd_monitor))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("stats", cmd_stats))
    # Auto-detect only in private chats (groups require /commands)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_message))
    # Track group joins / removals
    app.add_handler(ChatMemberHandler(handle_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))

    # Start background poller and trending monitor after event loop is running
    async def _post_init(_app: Application) -> None:
        ensure_poller_started()
        start_monitor(_app)

    app.post_init = _post_init

    logger.info("Bot is ready. Polling for updates…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
