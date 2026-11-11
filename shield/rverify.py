"""Public verifier for the R oracle's no-repaint proof.

    python -m shield.rverify [--node http://127.0.0.1:8790] [--tamper]

1. Download every closed bar the node has published (/r/history).
2. Recompute the hash chain from genesis — any edited R value changes every link after it.
3. For each anchor (/r/anchors): the recomputed link at that bar must equal the anchored link,
   and that link must exist on NEAR with the block time it was committed at.
4. Check the live /r response is signed by the node key.
--tamper flips one anchored R value by 0.0001 locally first, to show the proof catching it.
"""
import argparse
import json
import random

import requests

from .chain import get_chain
from .rproof import GENESIS, link_of


def verify(node: str, tamper: bool = False) -> dict:
    bars, i = [], 0
    while True:
        page = requests.get(f"{node}/r/history", params=dict(from_i=i, limit=20000), timeout=30).json()
        bars += page["bars"]
        i = len(bars)
        if i >= page["count"] or not page["bars"]:
            break
    anchors = requests.get(f"{node}/r/anchors", timeout=30).json()["anchors"]

    tampered = None
    if tamper and anchors:
        k = random.randrange(0, anchors[-1]["i"] + 1)
        bars[k]["r"] = round(bars[k]["r"] + 0.0001, 6)
        tampered = k

    links, prev, first_bad = [], GENESIS, None
    for b in bars:
        bar = {k: v for k, v in b.items() if k != "link"}
        prev = link_of(prev, bar)
        links.append(prev)
        if first_bad is None and prev != b["link"]:
            first_bad = b["i"]

    chain, checked = get_chain(), []
    for a in anchors:
        ok_link = a["i"] < len(links) and links[a["i"]] == a["link"]
        onchain = chain.get(a["link"])
        checked.append(dict(i=a["i"], link_matches=ok_link, on_near=onchain is not None,
                            near_ts=onchain and onchain["ts"]))

    live = requests.get(f"{node}/r", timeout=30).json()
    from eth_account import Account
    from eth_account.messages import encode_defunct
    signer = Account.recover_message(encode_defunct(text=live["signed"]), signature=live["signature"])
    key = requests.get(f"{node}/r/key", timeout=30).json()["signer"]

    anchored = [c for c in checked if c["link_matches"] and c["on_near"]]
    return dict(
        bars=len(bars), chain_intact=first_bad is None, first_mismatch_at=first_bad,
        tampered_bar=tampered, anchors=len(checked), anchors_verified=len(anchored),
        provably_unrepainted_through=max((c["i"] for c in anchored), default=None),
        latest_anchor=checked[-1] if checked else None,
        live_r=live["data"]["closed"]["r"], live_signature_ok=signer.lower() == key.lower(),
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--node", default="http://127.0.0.1:8790")
    ap.add_argument("--tamper", action="store_true")
    args = ap.parse_args()
    print(json.dumps(verify(args.node, args.tamper), indent=2))


if __name__ == "__main__":
    main()
