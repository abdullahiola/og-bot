"""Format TokenResult lists into Telegram-friendly HTML messages."""

from __future__ import annotations

from datetime import datetime, timezone

from models import TokenResult


def _rank_emoji(rank: int) -> str:
    if rank == 1:
        return "🥇"
    if rank == 2:
        return "🥈"
    if rank == 3:
        return "🥉"
    return f"#{rank}"


def _truncate_mint(mint: str) -> str:
    if len(mint) <= 12:
        return mint
    return f"{mint[:4]}…{mint[-4:]}"


def _format_date(iso: str | None) -> str:
    if not iso:
        return "unknown"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso[:16] if iso else "unknown"


def _format_usd(val: float | None) -> str:
    if val is None:
        return "—"
    if val >= 1_000_000:
        return f"${val / 1_000_000:.1f}M"
    if val >= 1_000:
        return f"${val / 1_000:.1f}K"
    return f"${val:.0f}"


def format_search_results(
    results: list[TokenResult],
    query: str,
    mode: str | None = None,
    timing: float | None = None,
    total_found: int | None = None,
    scan_name: str | None = None,
    scan_symbol: str | None = None,
    scanned_mint: str | None = None,
    is_scanned_og: bool | None = None,
    scanned_rank: int | None = None,
    subscriber_line: str | None = None,
) -> str:
    """Build an HTML-formatted Telegram message from search results."""
    lines: list[str] = []

    # Header
    if mode == "scan" and scanned_mint:
        name = scan_name or "Unknown"
        symbol = scan_symbol or "???"
        lines.append(f"🔍 <b>Mint Scan: {name} (${symbol})</b>")
        if is_scanned_og:
            lines.append("✅ <b>This is the OG!</b>")
        elif scanned_rank:
            lines.append(f"📊 Rank: #{scanned_rank} out of {len(results)}")
        lines.append("")
    elif mode == "social":
        lines.append(f"🔗 <b>Social Link Search</b>")
        lines.append(f"<code>{_truncate_query(query, 60)}</code>")
        lines.append("")
    else:
        lines.append(f'🏆 <b>OG Search: "{_truncate_query(query, 40)}"</b>')
        lines.append("")

    if not results:
        lines.append("No tokens found.")
        if timing:
            lines.append(f"\n⏱ {timing / 1000:.1f}s")
        return "\n".join(lines)

    # Show top results (max 10 for readability)
    show_count = min(len(results), 10)
    for token in results[:show_count]:
        emoji = _rank_emoji(token.rank)
        label = token.rank_label or ""

        line = f"{emoji} <b>{label}</b> — {_escape_html(token.display_name)} (${_escape_html(token.display_symbol)})"
        if token.is_scanned:
            line += " 👈"
        lines.append(line)

        # Date
        lines.append(f"  📅 {_format_date(token.created_at)} · via {token.time_source}")

        # Market data for social/volume modes
        if token.ranking_mode in ("volume", "marketcap"):
            parts = []
            if token.market_cap_usd:
                parts.append(f"MC: {_format_usd(token.market_cap_usd)}")
            elif token.fdv_usd:
                parts.append(f"FDV: {_format_usd(token.fdv_usd)}")
            if token.volume_usd_24h:
                parts.append(f"Vol: {_format_usd(token.volume_usd_24h)}")
            if parts:
                lines.append(f"  💰 {' · '.join(parts)}")

        # Mint address (tap to copy in Telegram)
        lines.append(f'  🔗 <code>{token.mint}</code>')
        lines.append(f'  📎 <a href="https://solscan.io/token/{token.mint}">Solscan</a>')

        # Confidence
        stars = "⭐" * min(token.confidence, 5)
        if not stars:
            stars = "☆"
        lines.append(f"  {stars} {token.confidence_label}")
        lines.append("")

    # Footer
    remaining = len(results) - show_count
    if remaining > 0:
        lines.append(f"<i>… and {remaining} more</i>")
        lines.append("")

    footer_parts = []
    if timing:
        footer_parts.append(f"⏱ {timing / 1000:.1f}s")
    count = total_found if total_found is not None else len(results)
    footer_parts.append(f"{count} tokens found")
    lines.append(" · ".join(footer_parts))

    if subscriber_line:
        lines.append(subscriber_line)

    return "\n".join(lines)


def _truncate_query(q: str, max_len: int) -> str:
    if len(q) <= max_len:
        return q
    return q[:max_len - 1] + "…"


def _escape_html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
