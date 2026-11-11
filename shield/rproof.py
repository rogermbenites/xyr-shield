"""The R oracle's chain rule — all a verifier needs (no R formula, no private code)."""
import hashlib
import json

GENESIS = "0" * 64


def canonical(x) -> str:
    return json.dumps(x, sort_keys=True, separators=(",", ":"))


def link_of(prev: str, bar: dict) -> str:
    """link_i = sha256(link_{i-1} : canonical(bar_i)) — editing any bar breaks every link after it."""
    return hashlib.sha256(f"{prev}:{canonical(bar)}".encode()).hexdigest()
