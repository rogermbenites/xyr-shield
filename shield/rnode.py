"""R oracle: live R, signed, with a no-repaint proof on NEAR.

The R model and its settings stay private (see rmodel.py — loaded by name, never in this repo);
only values are published. Every CLOSED bar is appended to a hash chain
(link = sha256(prev_link : bar)) and the chain head is anchored on NEAR every RNODE_ANCHOR_S
seconds. Once an anchor covers bar i, nobody — including the operator — can change any R value
up to i without the recomputed links failing to match the on-chain hash.

    python -m shield.rnode                 # warm up, follow the source, serve on :8790

Public  GET /r              live R (closed + forming), signed by the node key
        GET /r/history      closed bars with their chain links (?from_i=&limit=)
        GET /r/anchors      chain heads committed to NEAR, with tx links
        GET /r/key          the node's signing address
Verify: python -m shield.rverify
"""
import argparse
import asyncio
import json
import os
import secrets
import threading
import time
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from . import DATA
from .chain import get_chain
from .rmodel import BinanceKlines, DemoR, load
from .rproof import GENESIS, canonical, link_of

ANCHOR_S = float(os.environ.get("RNODE_ANCHOR_S", "300"))
EXPLORER = "https://testnet.nearblocks.io"


class RChain:
    """Append-only closed-bar log on disk. Each line = bar fields + its link."""

    def __init__(self, path=DATA / "r_chain.jsonl"):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path, self.lock = path, threading.Lock()
        self.rows = [json.loads(l) for l in open(path)] if path.exists() else []

    @property
    def head(self) -> str:
        return self.rows[-1]["link"] if self.rows else GENESIS

    @property
    def last_end(self) -> int:
        return self.rows[-1]["end_ms"] if self.rows else 0

    def append(self, end_ms, o, h, l, c, r, mode) -> dict:
        with self.lock:
            bar = dict(i=len(self.rows), end_ms=int(end_ms), o=o, h=h, l=l, c=c,
                       r=round(r, 6), mode=mode)
            row = dict(bar, link=link_of(self.head, bar))
            with open(self.path, "a") as f:
                f.write(json.dumps(row) + "\n")
            self.rows.append(row)
            return row

    def slice(self, from_i, limit):
        with self.lock:
            return self.rows[from_i:from_i + limit]


class Anchors:
    def __init__(self, path=DATA / "r_anchors.jsonl"):
        self.path = path
        self.rows = [json.loads(l) for l in open(path)] if path.exists() else []

    def add(self, row):
        with open(self.path, "a") as f:
            f.write(json.dumps(row) + "\n")
        self.rows.append(row)


class Signer:
    """secp256k1 node key; responses are EIP-191 signed so any ethers/eth_account client can verify."""

    def __init__(self, path=DATA / "node_key"):
        from eth_account import Account
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text("0x" + secrets.token_hex(32))
            path.chmod(0o600)
        self.acct = Account.from_key(path.read_text().strip())
        self.address = self.acct.address

    def sign(self, payload: dict) -> dict:
        from eth_account.messages import encode_defunct
        msg = canonical(payload)
        sig = self.acct.sign_message(encode_defunct(text=msg)).signature.hex()
        return dict(data=payload, signed=msg, signature=sig if sig.startswith("0x") else "0x" + sig,
                    signer=self.address)


class ROracle:
    def __init__(self):
        self.model = load("RNODE_MODEL", DemoR)
        self.source = load("RNODE_SOURCE", BinanceKlines)
        self.count = 0
        self.last_c = None
        self.chain = RChain()
        self.anchors = Anchors()

    def _close(self, bar):
        r, mode = self.model.update(bar["c"])
        self.count += 1
        self.last_c = bar["c"]
        if self.count >= self.model.warmup_bars and bar["end_ms"] > self.chain.last_end:
            return self.chain.append(bar["end_ms"], bar["o"], bar["h"], bar["l"], bar["c"], r, mode)

    def warmup(self):
        new = sum(self._close(b) is not None for b in self.source.warmup())
        print(f"  R primed: {self.count} bars · R={getattr(self.model, 'r', 0) or 0:+.4f} · chain {len(self.chain.rows)} bars "
              f"({new} new from warmup)", flush=True)

    def on_close(self, bar):
        row = self._close(bar)
        if row:
            print(f"  R #{row['i']} close={row['c']:,.1f} R={row['r']:+.4f} {row['mode']} "
                  f"link={row['link'][:12]}…", flush=True)

    def on_tick(self, price):
        self.last_c = price

    def forming_r(self):
        """R if the forming bar closed now — informational, NOT anchored (it repaints by nature)."""
        p = getattr(self.source, "price", None)
        return None if p is None or self.count == 0 else round(self.model.peek(p), 6)

    def feed_forever(self):
        while True:
            try:
                asyncio.run(self.source.stream(self.on_close, self.on_tick))
            except Exception as e:
                print(f"⚠️  feed dropped ({type(e).__name__}: {e}); reconnecting in 5s…", flush=True)
            time.sleep(5)

    def anchor_forever(self):
        """Every ANCHOR_S: anchor the chain head on NEAR if a bar closed since the last anchor,
        and always log the current R."""
        chain = get_chain()
        next_beat = time.time()
        while True:
            if time.time() >= next_beat and self.chain.rows:
                next_beat = time.time() + ANCHOR_S
                head = self.chain.rows[-1]
                last = self.anchors.rows[-1] if self.anchors.rows else None
                fr = self.forming_r()
                forming = f" · forming R={fr:+.4f}" if fr is not None else ""
                closed = f"R #{head['i']} R={head['r']:+.4f} {head['mode']} close={head['c']:,.1f}"
                if last is None or last["link"] != head["link"]:
                    try:
                        rec = chain.commit(head["link"])
                        self.anchors.add(dict(i=head["i"], link=head["link"], r=head["r"], mode=head["mode"],
                                              close=head["c"], end_ms=head["end_ms"], n=rec["n"], ts=rec["ts"],
                                              ref=rec.get("ref"), at=time.time()))
                        print(f"  ⚓ anchored {closed} → {chain.name} n={rec['n']} {rec.get('ref')}{forming}",
                              flush=True)
                    except Exception as e:
                        print(f"  ⚠️  anchor failed ({closed}): {e}", flush=True)
                        next_beat = time.time() + 30   # network blip — retry soon
                else:
                    print(f"  · {closed} — no new bar since anchor n={last['n']}{forming}", flush=True)
            time.sleep(5)


oracle: ROracle = None
signer: Signer = None
app = FastAPI(title="XYR R Oracle", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])


def _anchor_out(a):
    return dict(a, tx_url=f"{EXPLORER}/txns/{a['ref']}" if a.get("ref") else None)


@app.get("/r")
def r_now():
    if oracle is None or not oracle.chain.rows:
        raise HTTPException(503, "warming up")
    last = oracle.chain.rows[-1]
    anc = oracle.anchors.rows[-1] if oracle.anchors.rows else None
    payload = dict(
        symbol="BTCUSD_PERP", asof=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        closed=dict(i=last["i"], end=datetime.fromtimestamp(last["end_ms"] / 1000, timezone.utc)
                    .isoformat(timespec="seconds"), close=last["c"], r=last["r"], mode=last["mode"],
                    link=last["link"]),
        forming=dict(pct=round(100 * oracle.source.progress()), price=getattr(oracle.source, "price", None),
                     r=oracle.forming_r(), note="forming value repaints until the bar closes; not anchored"),
        anchored_through=anc["i"] if anc else None,
    )
    return signer.sign(payload)


@app.get("/r/history")
def r_history(from_i: int = 0, limit: int = 5000):
    rows = oracle.chain.slice(from_i, min(limit, 20000))
    return dict(genesis=GENESIS, from_i=from_i, count=len(oracle.chain.rows), bars=rows)


@app.get("/r/anchors")
def r_anchors():
    contract = os.environ.get("SHIELD_CONTRACT") or os.environ.get("NEAR_ACCOUNT_ID")
    return dict(contract=contract, contract_url=f"{EXPLORER}/address/{contract}",
                anchors=[_anchor_out(a) for a in oracle.anchors.rows])


@app.get("/r/key")
def r_key():
    return dict(signer=signer.address, scheme="EIP-191 personal_sign over the canonical JSON in `signed`")


def main():
    global oracle, signer
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8790)
    args = ap.parse_args()
    signer = Signer()
    oracle = ROracle()
    oracle.warmup()
    threading.Thread(target=oracle.feed_forever, daemon=True).start()
    threading.Thread(target=oracle.anchor_forever, daemon=True).start()
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
