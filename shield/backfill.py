"""Load past records into the vault so the API has history to answer from.

    python -m shield.backfill --file data.txt    # import a strategy's past data log
    python -m shield.backfill --flush               # commit any vaulted-but-uncommitted hashes

Backfilled rows are marked source=backfill. Their chain timestamp is TODAY, so the
verifier reports them as late commits — they prove the record wasn't edited after
today, not that the trades were called in real time. Only live rows prove that.
"""
import argparse
import re

from .chain import get_chain
from .vault import Vault

# tag DATA (SIGNAL still accepted for older logs; TICK = intrabar records)
LINE = re.compile(r"(?P<tag>DATA|SIGNAL|TICK)\s*\| (?P<kind>\w+)\s*\| (?P<ts>[\d\- :]+)Z \| price=(?P<price>[\d,.]+) \| "
                  r"R=(?P<r>[+\-\d.]+)(?: \| Rmode=(?P<rmode>\w+))?(?P<rest>.*)")


def parse_line(line, source="backfill", ticks=False):
    """One data-log line → vault kwargs. TICK (per-tick fire) lines only when ticks=True."""
    m = LINE.match(line.strip())
    if not m or (m["tag"] == "TICK" and not ticks):
        return None
    reason = m["rest"].split("|")[-1].strip() or None
    return dict(kind=m["kind"], ts=m["ts"].strip().replace(" ", "T") + "+00:00",
                price=float(m["price"].replace(",", "")), r=float(m["r"]), rmode=m["rmode"],
                reason=reason, source=source)


def parse(path):
    for line in open(path):
        s = parse_line(line)
        if s:
            yield s


def flush(vault, chain):
    rows = vault.uncommitted()
    for i, row in enumerate(rows):
        rec = chain.commit(row["hash"])
        vault.mark_committed(row["hash"], chain.name, rec)
        if i % 25 == 0 or i == len(rows) - 1:
            print(f"  committed {i + 1}/{len(rows)} → {chain.name}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="data log to import (same line format the server follows)")
    ap.add_argument("--flush", action="store_true", help="only commit pending hashes")
    args = ap.parse_args()
    vault, chain = Vault(), get_chain()
    if not args.flush:
        if not args.file:
            raise SystemExit("--file is required (or --flush)")
        if vault.next_seq() > 0:
            raise SystemExit("vault is not empty — backfill only into a fresh vault (rm data/vault.db)")
        sigs = sorted(parse(args.file), key=lambda s: s["ts"])
        for s in sigs:
            vault.add(**s)
        print(f"vaulted {len(sigs)} records from {args.file}")
    flush(vault, chain)


if __name__ == "__main__":
    main()
