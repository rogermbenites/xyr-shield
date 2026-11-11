"""Public verifier: anyone holding a reveal bundle can check it against the chain.

    python -m shield.verify bundle.json [--max-lag 120]

Checks: hash == sha256(payload, salt); hash exists on chain; the chain recorded
it within --max-lag seconds of the record time (i.e. called live, not backdated).
"""
import argparse
import json
from datetime import datetime

from .chain import get_chain
from .vault import commitment


def verify(bundle: dict, max_lag: float = 120) -> dict:
    p = bundle["payload"]
    recomputed = commitment(p, bundle["salt"])
    on_chain = get_chain().get(recomputed)
    out = dict(seq=p["seq"], kind=p["kind"], data_ts=p["ts"],
               hash_ok=recomputed == bundle["hash"], on_chain=on_chain is not None)
    if on_chain:
        lag = (datetime.fromisoformat(on_chain["ts"].replace("Z", "+00:00"))
               - datetime.fromisoformat(p["ts"].replace("Z", "+00:00"))).total_seconds()
        out.update(chain_n=on_chain["n"], chain_ts=on_chain["ts"], lag_s=round(lag, 1),
                   called_live=0 <= lag <= max_lag)
    out["valid"] = out["hash_ok"] and out["on_chain"]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bundle")
    ap.add_argument("--max-lag", type=float, default=120)
    args = ap.parse_args()
    print(json.dumps(verify(json.load(open(args.bundle)), args.max_lag), indent=2))


if __name__ == "__main__":
    main()
