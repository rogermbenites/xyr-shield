"""Hyperliquid TESTNET executor: turns each record into a real (test-money) BTC perp fill.

Position-target model — each record says where the position should be, we trade the difference:
    ENTRY_LONG → +SIZE   ENTRY_SHORT → -SIZE   EXIT_* → 0
so a repeated or late record is a no-op, and a manual/stray position is corrected on the next one.

Orders are IOC limits a little through the mid (fill now or cancel — nothing rests on the book).
Info calls go over direct HTTP; spot_meta is filtered so the SDK accepts it.

env: HL_ACCOUNT_ADDRESS (wallet holding the funds), HL_AGENT_KEY (API/agent wallet private key),
     HL_SIZE (BTC, default 0.0002 ≈ $15), SHIELD_EXEC=on to enable.
"""
import os
import time

import requests

URL = "https://api.hyperliquid-testnet.xyz"   # hard-coded: this executor never touches mainnet
COIN = "BTC"
SLIP = 0.005                                    # IOC limit 0.5% through mid


def enabled() -> bool:
    return os.environ.get("SHIELD_EXEC", "off") == "on"


def _info(payload):
    r = requests.post(f"{URL}/info", json=payload, timeout=20)
    r.raise_for_status()
    return r.json()


class Executor:
    def __init__(self):
        from eth_account import Account
        from hyperliquid.exchange import Exchange
        self.address = os.environ["HL_ACCOUNT_ADDRESS"]
        self.size = float(os.environ.get("HL_SIZE", "0.0002"))
        spot = _info({"type": "spotMeta"})
        n = len(spot.get("tokens", []))
        spot = {**spot, "universe": [u for u in spot.get("universe", [])
                                     if max(u.get("tokens", [n, n])) < n]}
        self.ex = Exchange(Account.from_key(os.environ["HL_AGENT_KEY"]), base_url=URL,
                           meta=_info({"type": "meta"}), spot_meta=spot, account_address=self.address)

    def position(self) -> float:
        st = _info({"type": "clearinghouseState", "user": self.address})
        for p in st.get("assetPositions", []):
            if p["position"]["coin"] == COIN:
                return float(p["position"]["szi"])
        return 0.0

    def target(self, kind: str) -> float:
        return {"ENTRY_LONG": self.size, "ENTRY_SHORT": -self.size}.get(kind, 0.0)

    def execute(self, kind: str) -> dict:
        """Trade to the record's target position. Returns the fill record that gets vaulted."""
        before = self.position()
        delta = round(self.target(kind) - before, 5)
        if abs(delta) < 1e-5:
            return dict(venue="hl-testnet", status="noop", pos=before)
        is_buy = delta > 0
        mid = float(_info({"type": "allMids"})[COIN])
        px = round(mid * (1 + SLIP if is_buy else 1 - SLIP))
        res = self.ex.order(COIN, is_buy, abs(delta), px, {"limit": {"tif": "Ioc"}},
                            reduce_only=self.target(kind) == 0)
        st = (res.get("response") or {}).get("data", {}).get("statuses", [{}])[0] \
            if res.get("status") == "ok" else {"error": str(res.get("response", res))}
        if "filled" not in st:
            return dict(venue="hl-testnet", status="rejected", error=st.get("error", str(st)), pos=before)
        f = st["filled"]
        sz = float(f["totalSz"])   # IOC can partially fill — record what actually traded
        return dict(venue="hl-testnet", status="filled", oid=f["oid"], side="buy" if is_buy else "sell",
                    sz=sz, px=float(f["avgPx"]), fee=self._fee(f["oid"]), mid=mid,
                    pos=round(before + (sz if is_buy else -sz), 5))

    def _fee(self, oid, tries=4):
        for _ in range(tries):   # fills index a moment after the order returns
            for f in _info({"type": "userFills", "user": self.address}):
                if f.get("oid") == oid:
                    return float(f["fee"])
            time.sleep(0.5)
        return None
