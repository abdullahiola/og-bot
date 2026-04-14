"""Solana address utilities — port of solana.ts."""

import re

# Base58 Solana address: typically 32–44 chars, no 0 O I l
_BASE58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]+$")


def is_likely_mint_address(s: str) -> bool:
    t = s.strip()
    if len(t) < 32 or len(t) > 44:
        return False
    return bool(_BASE58.match(t))
