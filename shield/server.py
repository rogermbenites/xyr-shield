"""XYR Shield node: shielded trade log + private stats API.

Any strategy can feed it: append one line per data point to a log file and point the node at it.
The strategy itself never touches this repo, so its logic stays private. Data only — no advice.

    DATA   | ENTRY_LONG | 2026-11-11 15:47:13Z | price=73794.4 | R=+0.0889 | Rmode=HOLD | reason

    python -m shield.server --data-file data.txt          # follow a strategy's data log, API on :8787
    python -m shield.server                               # API only, over whatever is in the vault

Public    GET  /log            commitment hashes + chain receipts (no trade data)
Key       GET  /stats?...      aggregate stats over the hidden log
          POST /ask            {"question": "..."} → attested-LLM answer + TEE receipts
Local     GET  /owner-log      trades + NEAR tx ids for the owner's dashboard (loopback only)
Admin     POST /demo-record    commit a record now (source=demo, excluded from stats)
          GET  /reveal/{seq}   reveal bundle for one trade (owner only)
"""
import argparse
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .chain import get_chain
from .vault import Vault
from . import stats as stats_mod

vault = Vault()
chain = get_chain()
app = FastAPI(title="XYR Shield", version="0.1.0")
# the owner's local dashboard reads /owner-log from the browser
app.add_middleware(CORSMiddleware, allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
                   allow_methods=["GET"], allow_headers=["*"])
last_seen = None   # last record read from the log

API_KEYS = {k for k in os.environ.get("SHIELD_API_KEYS", "").split(",") if k}
ADMIN_KEY = os.environ.get("SHIELD_ADMIN_KEY", "")


_executor = None


def execute(kind: str):
    """SHIELD_EXEC=on → trade the record on Hyperliquid testnet; the fill is vaulted with it."""
    global _executor
    from . import executor
    if not executor.enabled():
        return None
    try:
        _executor = _executor or executor.Executor()
        fill = _executor.execute(kind)
    except Exception as e:   # never lose the record because the venue failed
        fill = dict(venue="hl-testnet", status="error", error=f"{type(e).__name__}: {e}")
    print(f"  💱 {kind} → {fill.get('status')} "
          + (f"{fill['side']} {fill['sz']} @ {fill['px']:,.1f} oid={fill['oid']}" if fill.get("status") == "filled"
             else fill.get("error", f"pos={fill.get('pos')}")), flush=True)
    return fill


def publish(sig: dict):
    """Trade it (if enabled), vault the record + fill privately, push only the hash to the chain."""
    fill = execute(sig["kind"])
    c = vault.add(**sig, fill=fill)
    try:
        rec = chain.commit(c["hash"])
        vault.mark_committed(c["hash"], chain.name, rec)
        print(f"  🔒 seq={c['seq']} {sig['kind']} → {chain.name} n={rec['n']} {c['hash'][:16]}…", flush=True)
    except Exception as e:   # vault keeps it; retry_pending() picks it up
        print(f"  ⚠️  chain commit failed for seq={c['seq']}: {e}", flush=True)


def retry_pending(every_s: float = 20):
    """Re-commit vaulted-but-unpublished hashes (network blips). Check the chain first: a tx can
    land even when its reply is lost, and the contract rejects a second commit of the same hash."""
    while True:
        time.sleep(every_s)
        for row in vault.uncommitted():
            try:
                rec = chain.get(row["hash"]) or chain.commit(row["hash"])
                vault.mark_committed(row["hash"], chain.name, rec)
                print(f"  🔒 seq={row['seq']} {row['kind']} → {chain.name} n={rec['n']} (retried)", flush=True)
            except Exception as e:
                print(f"  ⏳ seq={row['seq']} still pending: {e}", flush=True)
                break   # network still down — try again next round


# ── auth ─────────────────────────────────────────────────────────────────────
def need_key(x_api_key: str = Header(default="")):
    if x_api_key not in API_KEYS and x_api_key != ADMIN_KEY:
        raise HTTPException(401, "API key required (X-Api-Key)")


def need_admin(x_api_key: str = Header(default="")):
    if not ADMIN_KEY or x_api_key != ADMIN_KEY:
        raise HTTPException(403, "admin only")


# ── routes ───────────────────────────────────────────────────────────────────
@app.get("/log")
def log():
    rows = vault.public_log()
    return dict(chain=chain.name, count=len(rows), commitments=rows)


@app.get("/stats", dependencies=[Depends(need_key)])
def get_stats(side: str = "all", r_min: Optional[float] = None, r_max: Optional[float] = None,
              abs_r: bool = False, rmode: Optional[str] = None, since_days: Optional[float] = None,
              fee_bp: float = 0.0):
    return stats_mod.query(vault.records(), side, r_min, r_max, abs_r, rmode, since_days, fee_bp)


class Ask(BaseModel):
    question: str
    receipts: bool = True


@app.post("/ask", dependencies=[Depends(need_key)])
def post_ask(body: Ask):
    from .ask import ask
    return ask(body.question, vault.records(), with_receipts=body.receipts)


@app.post("/demo-record", dependencies=[Depends(need_admin)])
def demo_record(kind: str = "ENTRY_LONG", price: float = 0.0):
    """Commit a record on demand (source=demo, excluded from stats) — for showing a live commit."""
    publish(dict(kind=kind, price=price, r=None, rmode=None, reason="demo", source="demo",
                 ts=datetime.now(timezone.utc).isoformat(timespec="seconds")))
    return vault.public_log()[-1]


@app.get("/owner-log")
def owner_log(request: Request, limit: int = 50):
    """The owner's own view: trades WITH their NEAR receipts. Loopback only — never exposed."""
    if request.client is None or request.client.host not in ("127.0.0.1", "::1"):
        raise HTTPException(403, "owner log is local only")
    contract = os.environ.get("SHIELD_CONTRACT") or os.environ.get("NEAR_ACCOUNT_ID")
    explorer = "https://testnet.nearblocks.io" if os.environ.get("NEAR_NETWORK", "testnet") == "testnet" \
        else "https://nearblocks.io"
    rows = []
    for r in vault.rows(limit):
        lag = None
        if r["chain_ts"]:
            lag = round((datetime.fromisoformat(r["chain_ts"].replace("Z", "+00:00"))
                         - datetime.fromisoformat(r["ts"])).total_seconds(), 1)
        fill = json.loads(r["fill"]) if r["fill"] else None
        rows.append(dict(seq=r["seq"], kind=r["kind"], ts=r["ts"], price=r["price"], r=r["r"], fill=fill,
                         source=r["source"], hash=r["hash"], chain=r["chain"], chain_n=r["chain_n"],
                         chain_ts=r["chain_ts"], tx=r["chain_ref"], lag_s=lag,
                         tx_url=f"{explorer}/txns/{r['chain_ref']}" if r["chain"] == "near" and r["chain_ref"] else None))
    return dict(chain=chain.name, contract=contract,
                contract_url=f"{explorer}/address/{contract}" if chain.name == "near" and contract else None,
                count=vault.next_seq(), rows=rows)


@app.get("/reveal/{seq}", dependencies=[Depends(need_admin)])
def reveal(seq: int):
    b = vault.reveal(seq)
    if b is None:
        raise HTTPException(404, "no such seq")
    return b


# ── data source ────────────────────────────────────────────────────────────
def watch_file(path: str, ticks: bool):
    """Follow a strategy's data log; publish new lines only."""
    global last_seen
    from .backfill import parse_line
    p = Path(path)
    p.touch()
    with open(p) as f:
        f.seek(0, 2)   # history before startup is not "live" — backfill it separately if wanted
        while True:
            line = f.readline()
            if not line:
                time.sleep(0.5)
                continue
            sig = parse_line(line, source="live", ticks=ticks)
            if sig:
                last_seen = sig
                publish(sig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-file", help="follow this strategy data log")
    ap.add_argument("--ticks", action="store_true", help="also publish intrabar (TICK) fires")
    ap.add_argument("--port", type=int, default=8787)
    args = ap.parse_args()
    threading.Thread(target=retry_pending, daemon=True).start()
    if args.data_file:
        threading.Thread(target=watch_file, args=(args.data_file, args.ticks), daemon=True).start()
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
