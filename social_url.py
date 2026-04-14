"""Social/website URL parsing, matching, and DexScreener search term generation — port of social-url.ts."""

from __future__ import annotations

import re
from urllib.parse import urlparse


# ── Detection ──────────────────────────────────────────────────────────

def is_likely_social_url(s: str) -> bool:
    """Detect http(s) URL or bare domain paths for social/website link search."""
    t = s.strip()
    if len(t) < 8:
        return False
    if re.match(r"^https?://", t, re.IGNORECASE):
        return True
    if re.match(r"^[a-z0-9.-]+\.[a-z]{2,}/", t, re.IGNORECASE):
        return True
    return False


# ── Normalization ──────────────────────────────────────────────────────

def normalize_for_social_match(raw: str) -> str:
    """Canonical string for comparing DexScreener website/social URLs."""
    trimmed = raw.strip()
    if not trimmed:
        return ""
    try:
        with_proto = trimmed if re.match(r"^https?://", trimmed, re.IGNORECASE) else f"https://{trimmed}"
        u = urlparse(with_proto)
        host = (u.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if host == "twitter.com":
            host = "x.com"
        path = (u.path or "").rstrip("/").lower()
        if host == "x.com":
            path = re.sub(r"^/i/communities/", "/communities/", path)
        return f"{host}{path}"
    except Exception:
        out = re.sub(r"^https?://", "", trimmed, flags=re.IGNORECASE)
        out = re.sub(r"^www\.", "", out, flags=re.IGNORECASE)
        return out.lower()


# ── Target generation ─────────────────────────────────────────────────

RESERVED_X_SEGMENTS = {
    "i", "status", "communities", "intent", "search", "home",
    "hashtag", "explore", "notifications", "messages", "settings", "compose",
}


def _looks_like_x_handle(seg: str) -> bool:
    return bool(re.match(r"^[a-z0-9_]{3,15}$", seg, re.IGNORECASE)) and seg.lower() not in RESERVED_X_SEGMENTS


def social_match_targets(input_url: str) -> list[str]:
    """Build all target strings to match against DexScreener social/website URLs."""
    primary = normalize_for_social_match(input_url)
    if not primary:
        return []
    targets = [primary]
    try:
        trimmed = input_url.strip()
        with_proto = trimmed if re.match(r"^https?://", trimmed, re.IGNORECASE) else f"https://{trimmed}"
        u = urlparse(with_proto)
        host = (u.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if host == "twitter.com":
            host = "x.com"
        parts = [p.lower() for p in u.path.split("/") if p]

        if host in ("x.com", "twitter.com"):
            # Tweet URL → also match profile
            status_idx = -1
            for i, p in enumerate(parts):
                if i > 0 and p == "status":
                    status_idx = i
                    break
            if status_idx > 0:
                handle = parts[status_idx - 1]
                targets.append(f"x.com/{handle}")
                targets.append(f"twitter.com/{handle}")

            # Communities
            if "communities" in parts:
                comm_idx = parts.index("communities")
                if comm_idx + 1 < len(parts):
                    cid = parts[comm_idx + 1]
                    targets.append(f"x.com/communities/{cid}")
                    targets.append(f"x.com/i/communities/{cid}")

            # Handle from first path segment
            if status_idx < 0 and len(parts) >= 1 and _looks_like_x_handle(parts[0]):
                targets.append(f"x.com/{parts[0]}")
                targets.append(f"twitter.com/{parts[0]}")
    except Exception:
        pass

    return list(dict.fromkeys(targets))  # dedupe preserving order


# ── DexScreener search terms ──────────────────────────────────────────

def _host_label_prefixes(hostname: str) -> list[str]:
    label = hostname.split(".")[0].lower() if hostname else ""
    if len(label) < 4:
        return []
    out = []
    for length in range(4, min(13, len(label) + 1)):
        out.append(label[:length])
    return out


def _add_numeric_suffix_terms(seg: str, out: list[str]) -> None:
    if not re.match(r"^\d{8,}$", seg):
        return
    if len(seg) >= 8:
        out.append(seg[-8:])
    if len(seg) >= 12:
        out.append(seg[-12:])


def search_terms_for_dex(input_url: str) -> list[str]:
    """Generate DexScreener search terms ordered by likely usefulness."""
    high: list[str] = []
    medium: list[str] = []
    low: list[str] = []
    trimmed = input_url.strip()

    try:
        with_proto = trimmed if re.match(r"^https?://", trimmed, re.IGNORECASE) else f"https://{trimmed}"
        u = urlparse(with_proto)
        parts = [p for p in u.path.split("/") if p]
        host = (u.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        is_twitter = host in ("x.com", "twitter.com")

        if is_twitter:
            status_idx = -1
            for i, p in enumerate(parts):
                if i > 0 and p.lower() == "status":
                    status_idx = i
                    break
            if status_idx > 0:
                handle = parts[status_idx - 1]
                if _looks_like_x_handle(handle):
                    high.append(handle)
            for p in parts:
                if _looks_like_x_handle(p):
                    high.append(p)
            if parts:
                _add_numeric_suffix_terms(parts[-1], low)
            low.append("twitter")
            low.append("twitter.com")
        else:
            high.extend(_host_label_prefixes(host))
            if len(host) >= 3:
                medium.append(host)

        for p in parts:
            if len(p) >= 3 and p != "www" and p.lower() not in RESERVED_X_SEGMENTS and not re.match(r"^\d+$", p):
                medium.append(p)

        low.append(f"{host}{u.path}".rstrip("/"))
        low.append(re.sub(r"^https?://", "", trimmed, flags=re.IGNORECASE))
    except Exception:
        high.append(trimmed)

    all_terms = high + medium + low
    seen: set[str] = set()
    result: list[str] = []
    for t in all_terms:
        t = t.strip().lower()
        if len(t) >= 2 and t not in seen:
            seen.add(t)
            result.append(t)
    return result[:16]


# ── Link matching ─────────────────────────────────────────────────────

def _url_norm_path_prefix_match(stored: str, target: str) -> bool:
    if stored == target:
        return True
    if stored.startswith(target) and len(stored) > len(target) and stored[len(target)] == "/":
        return True
    if target.startswith(stored) and len(target) > len(stored) and target[len(stored)] == "/" and "/" in stored:
        return True
    return False


def link_matches_target(link_url: str, target_norms: list[str]) -> bool:
    """Check if a stored link URL matches ANY of the target variants."""
    n = normalize_for_social_match(link_url)
    if not n:
        return False
    for t in target_norms:
        if not t:
            continue
        if _url_norm_path_prefix_match(n, t):
            return True
    return False


# ── Birdeye search keywords ──────────────────────────────────────────

def birdeye_search_keywords(input_url: str) -> list[str]:
    """Keywords for Birdeye search: X handle or website host label."""
    trimmed = input_url.strip()
    try:
        with_proto = trimmed if re.match(r"^https?://", trimmed, re.IGNORECASE) else f"https://{trimmed}"
        u = urlparse(with_proto)
        host = (u.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if host == "twitter.com":
            host = "x.com"
        parts = [p.lower() for p in u.path.split("/") if p]

        if host == "x.com":
            if "communities" in parts:
                return []
            status_idx = -1
            for i, p in enumerate(parts):
                if i > 0 and p == "status":
                    status_idx = i
                    break
            if status_idx > 0:
                h = parts[status_idx - 1]
                if _looks_like_x_handle(h):
                    return [h]
            for p in parts:
                if _looks_like_x_handle(p):
                    return [p]
            return []

        label = host.split(".")[0] if host else ""
        if len(label) >= 3:
            return [label]
    except Exception:
        pass
    return []
