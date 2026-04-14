"""Social URL → token discovery via DexScreener profiles/boosts + Birdeye — port of dex-social.ts."""

from __future__ import annotations

import asyncio
import logging

import httpx

from models import RawToken, DEX_TIMEOUT, DEX_LIMIT
from normalize import dex_pair_created_ms
from social_url import link_matches_target, social_match_targets, search_terms_for_dex, birdeye_search_keywords
from url_index import search_by_url, upsert_token_links
from birdeye import get_birdeye_metadata_single, search_birdeye

logger = logging.getLogger("ogfinder")

DEX_SEARCH = "https://api.dexscreener.com/latest/dex/search"
TOKENS_V1 = "https://api.dexscreener.com/tokens/v1/solana"
TOKEN_PROFILES_LATEST = "https://api.dexscreener.com/token-profiles/latest/v1"
TOKEN_BOOSTS_LATEST = "https://api.dexscreener.com/token-boosts/latest/v1"

MAX_CANDIDATE_MINTS = 400
TOKEN_BATCH = 25
SEARCH_CONCURRENCY = 4


def _collect_link_strings(info: dict | None) -> list[str]:
    if not info:
        return []
    out: list[str] = []
    for w in info.get("websites") or []:
        if isinstance(w, dict) and w.get("url"):
            out.append(w["url"])
    for s in info.get("socials") or []:
        if isinstance(s, dict) and s.get("url"):
            out.append(s["url"])
    return out


def _pair_matches_target(pair: dict, targets: list[str]) -> bool:
    for link in _collect_link_strings(pair.get("info")):
        if link_matches_target(link, targets):
            return True
    return False


async def _fetch_dex_search(q: str) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=DEX_TIMEOUT) as client:
            resp = await client.get(DEX_SEARCH, params={"q": q})
            data = resp.json()
        pairs = data.get("pairs") or []
        return [p for p in pairs if isinstance(p, dict) and p.get("chainId") == "solana"]
    except Exception:
        return []


async def _fetch_token_pairs_batched(mints: list[str]) -> list[dict]:
    out: list[dict] = []
    for i in range(0, len(mints), TOKEN_BATCH):
        chunk = mints[i : i + TOKEN_BATCH]
        url = f"{TOKENS_V1}/{','.join(chunk)}"
        try:
            async with httpx.AsyncClient(timeout=max(DEX_TIMEOUT, 15)) as client:
                resp = await client.get(url)
                data = resp.json()
            if isinstance(data, list):
                out.extend(data)
        except Exception:
            continue
    return out


async def _find_mints_from_latest_profiles(targets: list[str]) -> list[str]:
    results: list[str] = []

    async def _scan_endpoint(endpoint: str) -> None:
        try:
            async with httpx.AsyncClient(timeout=DEX_TIMEOUT) as client:
                resp = await client.get(endpoint)
                data = resp.json()
            if not isinstance(data, list):
                return
            for tp in data:
                if not isinstance(tp, dict) or tp.get("chainId") != "solana":
                    continue
                urls = []
                for link in tp.get("links") or []:
                    if isinstance(link, dict) and link.get("url"):
                        urls.append(link["url"])
                # Index every profile
                if urls and tp.get("tokenAddress"):
                    try:
                        upsert_token_links(tp["tokenAddress"], urls, "dexscreener-profile")
                    except Exception:
                        pass
                # Check match
                for link in tp.get("links") or []:
                    if isinstance(link, dict) and link.get("url") and link_matches_target(link["url"], targets):
                        results.append(tp["tokenAddress"])
                        break
        except Exception:
            pass

    await asyncio.gather(
        _scan_endpoint(TOKEN_PROFILES_LATEST),
        _scan_endpoint(TOKEN_BOOSTS_LATEST),
    )
    return list(set(results))


def _pair_mc_usd(p: dict) -> float:
    return p.get("marketCap") or p.get("fdv") or 0


def _pair_beats(candidate: dict, existing: dict) -> bool:
    mc_c = _pair_mc_usd(candidate)
    mc_e = _pair_mc_usd(existing)
    if mc_c != mc_e:
        return mc_c > mc_e
    v_c = (candidate.get("volume") or {}).get("h24") or 0
    v_e = (existing.get("volume") or {}).get("h24") or 0
    if v_c != v_e:
        return v_c > v_e
    t_c = dex_pair_created_ms(candidate.get("pairCreatedAt")) or 0
    t_e = dex_pair_created_ms(existing.get("pairCreatedAt")) or 0
    return t_c > t_e


def _merge_pair_best(pair_map: dict[str, dict], p: dict, key_mint: str) -> None:
    existing = pair_map.get(key_mint)
    if not existing or _pair_beats(p, existing):
        pair_map[key_mint] = p


async def search_dex_by_social_url(input_url: str) -> list[RawToken]:
    """Find Solana pairs whose DexScreener profile includes the URL."""
    targets = social_match_targets(input_url)
    if not targets:
        return []

    terms = search_terms_for_dex(input_url)
    logger.debug("social targets=%s terms=%s", targets, terms)

    seen_pair_keys: set[str] = set()
    candidate_mint_set: set[str] = set()
    direct_hits: dict[str, dict] = {}

    # Channel 1: SQLite local index
    try:
        sqlite_mints = search_by_url(targets)
    except Exception:
        sqlite_mints = []

    # Channel 2+3: latest token profiles
    profile_mints_task = _find_mints_from_latest_profiles(targets)

    # Channel 4: Birdeye search
    async def _birdeye_search() -> list[str]:
        kws = birdeye_search_keywords(input_url)
        if not kws:
            return []
        out: set[str] = set()
        for kw in kws[:3]:
            try:
                items = await search_birdeye(kw)
                for it in items:
                    if isinstance(it, dict) and it.get("address"):
                        out.add(it["address"])
            except Exception:
                continue
        return list(out)

    birdeye_mints_task = _birdeye_search()

    # Channel 5: DexScreener name search
    for i in range(0, len(terms), SEARCH_CONCURRENCY):
        batch = terms[i : i + SEARCH_CONCURRENCY]
        batch_results = await asyncio.gather(*[_fetch_dex_search(t) for t in batch])
        for pairs in batch_results:
            for p in pairs:
                mint = (p.get("baseToken") or {}).get("address", "")
                if not mint:
                    continue
                key = p.get("pairAddress") or f"{mint}:{p.get('dexId', '')}"
                if key in seen_pair_keys:
                    continue
                seen_pair_keys.add(key)
                if p.get("info") and _pair_matches_target(p, targets):
                    _merge_pair_best(direct_hits, p, mint)
                candidate_mint_set.add(mint)
        if len(candidate_mint_set) >= MAX_CANDIDATE_MINTS:
            break

    # Merge all mint sources
    for m in sqlite_mints:
        candidate_mint_set.add(m)

    birdeye_mints = await birdeye_mints_task
    for m in birdeye_mints:
        candidate_mint_set.add(m)

    profile_mints = await profile_mints_task
    for m in profile_mints:
        candidate_mint_set.add(m)

    candidate_mints = list(candidate_mint_set)[:MAX_CANDIDATE_MINTS]

    # Trusted mints bypass re-check
    trusted_mints: set[str] = set(sqlite_mints) | set(profile_mints)

    by_mint: dict[str, dict] = dict(direct_hits)

    if candidate_mints:
        detailed_pairs = await _fetch_token_pairs_batched(candidate_mints)
        for p in detailed_pairs:
            if not isinstance(p, dict) or p.get("chainId") != "solana":
                continue
            mint = (p.get("baseToken") or {}).get("address", "")
            if not mint:
                continue
            is_trusted = mint in trusted_mints
            if not is_trusted and not _pair_matches_target(p, targets):
                continue
            _merge_pair_best(by_mint, p, mint)

    # Enrich unmatched trusted mints via Birdeye
    unmatched_trusted = [m for m in trusted_mints if m not in by_mint]
    if unmatched_trusted:
        async def _enrich(mint: str) -> None:
            try:
                urls = await get_birdeye_metadata_single(mint)
                if urls:
                    upsert_token_links(mint, urls, "birdeye-enrich")
            except Exception:
                pass
        await asyncio.gather(*[_enrich(m) for m in unmatched_trusted[:10]])

    # Index discovered links
    for mint, pair in by_mint.items():
        urls = _collect_link_strings(pair.get("info"))
        if urls:
            try:
                upsert_token_links(mint, urls, "dexscreener-search")
            except Exception:
                pass

    # Sort by MC → volume → time
    merged = sorted(by_mint.values(), key=lambda p: (-_pair_mc_usd(p), -((p.get("volume") or {}).get("h24") or 0)))

    tokens: list[RawToken] = []
    for pair in merged:
        if len(tokens) >= DEX_LIMIT:
            break
        base = pair.get("baseToken") or {}
        pair_ms = dex_pair_created_ms(pair.get("pairCreatedAt"))
        vol = (pair.get("volume") or {}).get("h24")
        tokens.append(RawToken(
            mint=base.get("address", ""),
            dex_name=base.get("name"),
            dex_symbol=base.get("symbol"),
            dex_id=pair.get("dexId"),
            pair_created_at=pair_ms,
            volume_usd_24h=vol if isinstance(vol, (int, float)) else None,
            dex_market_cap_usd=pair.get("marketCap") if isinstance(pair.get("marketCap"), (int, float)) else None,
            dex_fdv_usd=pair.get("fdv") if isinstance(pair.get("fdv"), (int, float)) else None,
        ))

    # Add unmatched trusted mints
    for mint in unmatched_trusted:
        if len(tokens) >= DEX_LIMIT:
            break
        if any(t.mint == mint for t in tokens):
            continue
        tokens.append(RawToken(mint=mint))

    return tokens
