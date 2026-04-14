"""Build enriched token results — port of enrich-results.ts + sort.ts."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from models import RawToken, TokenResult, HeliusSlotData, MAX_RESULTS
from helius import get_asset_batch, get_creation_slot
from normalize import normalize, dex_pair_created_ms

CREATION_SLOT_CONCURRENCY = 8


# ── Display name/symbol resolution ────────────────────────────────────

def resolve_display_name(
    dex_name: str | None = None,
    jup_name: str | None = None,
    helius_name: str | None = None,
) -> str:
    for name in (dex_name, jup_name, helius_name):
        if name and name not in ("Unknown", "???"):
            return name
    return "Unknown"


def resolve_display_symbol(
    dex_symbol: str | None = None,
    jup_symbol: str | None = None,
    helius_symbol: str | None = None,
) -> str:
    for symbol in (dex_symbol, jup_symbol, helius_symbol):
        if symbol and symbol not in ("???", "Unknown"):
            return symbol
    return "???"


# ── Sorting ────────────────────────────────────────────────────────────

def sort_by_creation_time(results: list[TokenResult]) -> list[TokenResult]:
    return sorted(results, key=lambda r: r.created_at_ms if r.created_at_ms is not None else float("inf"))


def sort_by_volume(results: list[TokenResult]) -> list[TokenResult]:
    return sorted(results, key=lambda r: -(r.volume_usd_24h or 0))


def sort_by_market_cap(results: list[TokenResult]) -> list[TokenResult]:
    def key(r: TokenResult) -> tuple[float, float, float]:
        mc = -(r.market_cap_usd or r.fdv_usd or 0)
        vol = -(r.volume_usd_24h or 0)
        t = -(r.created_at_ms or 0)
        return (mc, vol, t)
    return sorted(results, key=key)


# ── Confidence / Rank scoring ──────────────────────────────────────────

def score_confidence(results: list[TokenResult], query: str) -> list[TokenResult]:
    nq = normalize(query)
    with_times = [r for r in results if r.created_at_ms is not None]
    time_range = (
        (with_times[-1].created_at_ms or 0) - (with_times[0].created_at_ms or 0)
        if len(with_times) >= 2
        else 0
    )
    gap_to_second = (
        (with_times[1].created_at_ms or 0) - (with_times[0].created_at_ms or 0)
        if len(with_times) >= 2
        else 0
    )
    significant_gap = time_range > 0 and gap_to_second / time_range > 0.05

    scored = []
    for i, token in enumerate(results):
        score = 0
        name = normalize(token.display_name)
        symbol = normalize(token.display_symbol)
        if name == nq:
            score += 2
        if symbol == nq:
            score += 1
        if score < 3 and (nq in name or nq in symbol):
            score += 1
        if i == 0 and significant_gap:
            score += 1
        score = min(score, 5)

        if score >= 5:
            confidence_label = "Exact match"
        elif score >= 3:
            confidence_label = "Strong match"
        else:
            confidence_label = "Partial match"

        if i == 0:
            rank_label = "OG"
        else:
            rank_label = "Newer"

        scored.append(TokenResult(
            **{**token.__dict__, "confidence": score, "confidence_label": confidence_label,
               "rank": i + 1, "rank_label": rank_label}
        ))
    return scored


def score_volume_rank(results: list[TokenResult]) -> list[TokenResult]:
    scored = []
    for i, token in enumerate(results):
        if i == 0:
            cl, rl = "Top 24h volume", "Top"
        elif i < 3:
            cl, rl = "High volume", "High"
        else:
            cl, rl = "Lower volume", "—"
        scored.append(TokenResult(
            **{**token.__dict__, "ranking_mode": "volume", "confidence": max(1, 5 - min(i, 4)),
               "confidence_label": cl, "rank": i + 1, "rank_label": rl}
        ))
    return scored


def score_market_cap_rank(results: list[TokenResult]) -> list[TokenResult]:
    scored = []
    for i, token in enumerate(results):
        if i == 0:
            cl, rl = "Top market cap", "Top"
        elif i < 3:
            cl, rl = "High market cap", "High"
        else:
            cl, rl = "Lower market cap", "—"
        scored.append(TokenResult(
            **{**token.__dict__, "ranking_mode": "marketcap", "confidence": max(1, 5 - min(i, 4)),
               "confidence_label": cl, "rank": i + 1, "rank_label": rl}
        ))
    return scored


# ── Main enrichment pipeline ──────────────────────────────────────────

async def build_token_results(
    raw_tokens: list[RawToken],
    query_for_score: str,
    *,
    scanned_mint: str | None = None,
    rank_by: str = "creation",  # "creation" | "volume" | "marketcap"
) -> list[TokenResult]:
    mints = [t.mint for t in raw_tokens]
    helius_data = await get_asset_batch(mints)

    candidates: list[dict] = []

    for raw in raw_tokens:
        h = helius_data.get(raw.mint)
        is_scanned = scanned_mint is not None and raw.mint == scanned_mint

        if h:
            if h.token_interface and h.token_interface not in ("FungibleToken", "FungibleAsset"):
                continue
            if h.supply is not None and h.supply <= 0 and not is_scanned:
                continue

        created_at_ms: int | None = None
        slot: int | None = h.slot if h else None
        time_source = "unknown"

        if h and h.created_at:
            try:
                parsed = int(datetime.fromisoformat(h.created_at.replace("Z", "+00:00")).timestamp() * 1000)
                created_at_ms = parsed
                time_source = "helius"
            except Exception:
                pass

        pair_ms = dex_pair_created_ms(raw.pair_created_at)
        if pair_ms is not None:
            if created_at_ms is None or pair_ms < created_at_ms:
                created_at_ms = pair_ms
                time_source = "dexscreener"

        candidates.append({
            "raw": raw, "h": h, "is_scanned": is_scanned,
            "created_at_ms": created_at_ms, "slot": slot, "time_source": time_source,
        })

    # Resolve creation slots in batches
    sig_results = []
    for i in range(0, len(candidates), CREATION_SLOT_CONCURRENCY):
        chunk = candidates[i : i + CREATION_SLOT_CONCURRENCY]
        part = await asyncio.gather(*[get_creation_slot(c["raw"].mint) for c in chunk])
        sig_results.extend(part)

    enriched: list[TokenResult] = []
    for i, c in enumerate(candidates):
        sig = sig_results[i] if i < len(sig_results) else None
        created_at_ms = c["created_at_ms"]
        slot = c["slot"]
        time_source = c["time_source"]

        if sig:
            sig_ms = sig.get("blockTime", 0) * 1000
            if created_at_ms is None or sig_ms < created_at_ms:
                created_at_ms = sig_ms
                slot = sig.get("slot")
                time_source = "signatures"

        raw: RawToken = c["raw"]
        h: HeliusSlotData | None = c["h"]

        enriched.append(TokenResult(
            mint=raw.mint,
            display_name=resolve_display_name(raw.dex_name, raw.jup_name, h.helius_name if h else None),
            display_symbol=resolve_display_symbol(raw.dex_symbol, raw.jup_symbol, h.helius_symbol if h else None),
            slot=slot,
            created_at_ms=created_at_ms,
            created_at=datetime.fromtimestamp(created_at_ms / 1000, tz=timezone.utc).isoformat() if created_at_ms else None,
            dex_id=raw.dex_id,
            confidence=0,
            confidence_label="",
            rank=0,
            rank_label="",
            time_source=time_source,
            is_scanned=c["is_scanned"],
            volume_usd_24h=raw.volume_usd_24h,
            market_cap_usd=raw.dex_market_cap_usd,
            fdv_usd=raw.dex_fdv_usd,
            ranking_mode="marketcap" if rank_by == "marketcap" else ("volume" if rank_by == "volume" else "creation"),
        ))

    if rank_by == "volume":
        return score_volume_rank(sort_by_volume(enriched))[:MAX_RESULTS]
    if rank_by == "marketcap":
        return score_market_cap_rank(sort_by_market_cap(enriched))[:MAX_RESULTS]

    return score_confidence(sort_by_creation_time(enriched), query_for_score)[:MAX_RESULTS]
