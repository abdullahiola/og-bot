"""Data models and constants — port of types.ts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ── Constants ──────────────────────────────────────────────────────────
MAX_HELIUS = 150
MAX_RESULTS = 100
DEX_LIMIT = 100
JUP_LIMIT = 200
MIN_QUERY = 2
MAX_QUERY = 30
MAX_MINT_LEN = 44
MAX_SOCIAL_URL = 512

CACHE_SEARCH = 600
CACHE_DEX = 300
CACHE_JUP = 3600
CACHE_HELIUS = 3600

DEX_TIMEOUT = 5
HELIUS_TIMEOUT = 10
MAX_SIG_PAGES = 5


# ── Data classes ───────────────────────────────────────────────────────
@dataclass
class RawToken:
    mint: str
    dex_name: Optional[str] = None
    dex_symbol: Optional[str] = None
    jup_name: Optional[str] = None
    jup_symbol: Optional[str] = None
    dex_id: Optional[str] = None
    pair_created_at: Optional[int] = None  # ms timestamp
    volume_usd_24h: Optional[float] = None
    dex_market_cap_usd: Optional[float] = None
    dex_fdv_usd: Optional[float] = None


@dataclass
class HeliusSlotData:
    slot: Optional[int] = None
    created_at: Optional[str] = None
    helius_name: Optional[str] = None
    helius_symbol: Optional[str] = None
    token_interface: Optional[str] = None
    supply: Optional[float] = None


@dataclass
class TokenResult:
    mint: str
    display_name: str = "Unknown"
    display_symbol: str = "???"
    slot: Optional[int] = None
    created_at_ms: Optional[int] = None
    created_at: Optional[str] = None
    dex_id: Optional[str] = None
    confidence: int = 0
    confidence_label: str = ""
    rank: int = 0
    rank_label: str = ""
    time_source: str = "unknown"
    is_scanned: bool = False
    volume_usd_24h: Optional[float] = None
    market_cap_usd: Optional[float] = None
    fdv_usd: Optional[float] = None
    ranking_mode: str = "creation"  # "creation" | "volume" | "marketcap"


@dataclass
class SearchResponse:
    results: list[TokenResult] = field(default_factory=list)
    query: str = ""
    total_found: int = 0
    timing: Optional[float] = None
    mode: Optional[str] = None  # "search" | "scan" | "social"
    scanned_mint: Optional[str] = None
    scan_name: Optional[str] = None
    scan_symbol: Optional[str] = None
    is_scanned_og: Optional[bool] = None
    scanned_rank: Optional[int] = None
    error: Optional[str] = None
