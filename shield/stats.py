"""Aggregate stats over the private vault. Only aggregates ever leave this module.

Round trips are paired from the record stream the same way the strategy trades:
an ENTRY opens, the matching EXIT closes, and an opposite ENTRY while open
closes the old side at that price first (the engine reports exit+entry on the
same bar as just the entry).
"""
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

MIN_TRADES = 5   # refuse narrower slices: stops a buyer from isolating single trades


def _fill(s):
    """The venue fill for this record, if the executor traded it."""
    f = json.loads(s["fill"]) if "fill" in s.keys() and s["fill"] else None
    return f if f and f.get("status") == "filled" else None


def _fee_bp(f):
    return f["fee"] / (f["px"] * f["sz"]) * 1e4 if f and f.get("fee") is not None else 0.0


def round_trips(rows) -> list:
    """Priced at the real fill when there is one (slippage + fees included), else the recorded price."""
    trips, open_ = [], None
    for s in rows:
        f = _fill(s)
        kind, px = s["kind"], (f["px"] if f else s["price"])
        side = "long" if kind.endswith("LONG") else "short"
        if kind.startswith("EXIT"):
            if open_ and open_["side"] == side:
                trips.append(_close(open_, s))
                open_ = None
            continue
        if open_ and open_["side"] != side:
            trips.append(_close(open_, s))
            open_ = None
        if open_ is None:
            open_ = dict(side=side, entry_ts=s["ts"], entry_px=px, entry_r=s["r"], entry_rmode=s["rmode"],
                         paid_bp=_fee_bp(f))
    return trips


def _close(o, s) -> dict:
    sign = 1 if o["side"] == "long" else -1
    f = _fill(s)
    px = f["px"] if f else s["price"]
    return dict(o, exit_ts=s["ts"], exit_px=px, paid_bp=o["paid_bp"] + _fee_bp(f),
                gross_bp=sign * (px / o["entry_px"] - 1) * 1e4)


def _ts(x) -> datetime:
    return datetime.fromisoformat(x.replace("Z", "+00:00"))


def query(rows, side: str = "all", r_min: Optional[float] = None, r_max: Optional[float] = None,
          abs_r: bool = False, rmode: Optional[str] = None, since_days: Optional[float] = None,
          fee_bp: float = 0.0) -> dict:
    """Filter round trips by entry conditions, return aggregates only."""
    trips = round_trips(rows)
    if side in ("long", "short"):
        trips = [t for t in trips if t["side"] == side]
    if r_min is not None or r_max is not None:
        lo = -9 if r_min is None else r_min
        hi = 9 if r_max is None else r_max
        val = (lambda t: abs(t["entry_r"])) if abs_r else (lambda t: t["entry_r"])
        trips = [t for t in trips if t["entry_r"] is not None and lo <= val(t) <= hi]
    if rmode:
        trips = [t for t in trips if (t["entry_rmode"] or "").upper() == rmode.upper()]
    if since_days:
        cut = datetime.now(timezone.utc) - timedelta(days=since_days)
        trips = [t for t in trips if _ts(t["entry_ts"]) >= cut]

    filt = dict(side=side, r_min=r_min, r_max=r_max, abs_r=abs_r, rmode=rmode,
                since_days=since_days, fee_bp=fee_bp)
    n = len(trips)
    if n < MIN_TRADES:
        return dict(filter=filt, n=n, refused=f"fewer than {MIN_TRADES} trades match — too narrow to share")

    net = [t["gross_bp"] - t["paid_bp"] - fee_bp for t in trips]   # actual venue fees + any extra fee_bp
    wins = [x for x in net if x > 0]
    losses = [x for x in net if x <= 0]
    run = worst_run = 0
    for x in net:
        run = run + 1 if x <= 0 else 0
        worst_run = max(worst_run, run)
    return dict(
        filter=filt, n=n,
        win_rate=round(len(wins) / n, 4),
        profit_factor=round(sum(wins) / -sum(losses), 3) if losses and sum(losses) else None,
        avg_bp=round(sum(net) / n, 2),
        total_bp=round(sum(net), 1),
        max_losing_streak=worst_run,
        first=min(t["entry_ts"] for t in trips)[:10],
        last=max(t["exit_ts"] for t in trips)[:10],
    )
