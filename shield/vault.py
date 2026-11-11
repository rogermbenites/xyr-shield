"""Private vault: every record is stored here in full, with its salt.

Only the commitment hash leaves this file (to the chain). The payload + salt stay
private until the owner chooses to reveal one.

    hash = sha256( canonical_json(payload) + ":" + salt )

`seq` is part of the payload, so a complete reveal 0..N-1 against the on-chain
count proves no trade was dropped.
"""
import hashlib
import json
import secrets
import sqlite3
import threading
import time
from typing import Optional

from . import DATA

DB_PATH = DATA / "vault.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    seq          INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL,       -- ENTRY_LONG / ENTRY_SHORT / EXIT_LONG / EXIT_SHORT
    ts           TEXT NOT NULL,       -- record time, ISO-8601 UTC
    price        REAL NOT NULL,
    r            REAL,
    rmode        TEXT,
    reason       TEXT,
    source       TEXT NOT NULL,       -- live / backfill
    salt         TEXT NOT NULL,
    hash         TEXT NOT NULL UNIQUE,
    chain        TEXT,                -- local / near
    chain_n      INTEGER,
    chain_ts     TEXT,                -- when the chain recorded the hash
    chain_ref    TEXT,                -- tx hash on NEAR
    fill         TEXT                 -- JSON venue fill (executor on), part of the committed payload
);
"""


def canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def commitment(payload: dict, salt: str) -> str:
    return hashlib.sha256(f"{canonical(payload)}:{salt}".encode()).hexdigest()


def _locked(fn):
    def wrap(self, *a, **kw):
        with self.lock:
            return fn(self, *a, **kw)
    return wrap


class Vault:
    """One connection shared by the watcher, the retry loop and API threads — every call takes
    self.lock (unsynchronised use of one sqlite connection segfaulted the node, 2026-11-11)."""

    def __init__(self, path=DB_PATH):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        if "fill" not in {r[1] for r in self.db.execute("PRAGMA table_info(records)")}:
            self.db.execute("ALTER TABLE records ADD COLUMN fill TEXT")   # vaults made before the executor

    @_locked
    def next_seq(self) -> int:
        row = self.db.execute("SELECT COALESCE(MAX(seq) + 1, 0) FROM records").fetchone()
        return row[0]

    @_locked
    def add(self, kind, ts, price, r=None, rmode=None, reason=None, source="live", fill=None) -> dict:
        """Store a record privately; returns {seq, hash} — the only part that is published."""
        seq = self.next_seq()
        payload = dict(seq=seq, kind=kind, ts=ts, price=price, r=r, rmode=rmode, reason=reason)
        if fill is not None:   # only when present, so pre-executor hashes are unchanged
            payload["fill"] = fill
        salt = secrets.token_hex(16)
        h = commitment(payload, salt)
        self.db.execute(
            "INSERT INTO records (seq, kind, ts, price, r, rmode, reason, source, salt, hash, fill) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (seq, kind, ts, price, r, rmode, reason, source, salt, h,
             json.dumps(fill, sort_keys=True) if fill is not None else None))
        self.db.commit()
        return dict(seq=seq, hash=h)

    @_locked
    def mark_committed(self, h, chain, rec: dict):
        self.db.execute(
            "UPDATE records SET chain=?, chain_n=?, chain_ts=?, chain_ref=? WHERE hash=?",
            (chain, rec.get("n"), rec.get("ts"), rec.get("ref"), h))
        self.db.commit()

    @_locked
    def uncommitted(self):
        return self.db.execute("SELECT * FROM records WHERE chain IS NULL ORDER BY seq").fetchall()

    @_locked
    def records(self):
        """Real records only — demo commits are provable but never counted in stats."""
        return self.db.execute("SELECT * FROM records WHERE source != 'demo' ORDER BY seq").fetchall()

    @_locked
    def public_log(self):
        """What anyone may see: hashes, sequence numbers, chain receipts. No trade data."""
        rows = self.db.execute(
            "SELECT seq, hash, chain, chain_n, chain_ts, chain_ref FROM records ORDER BY seq").fetchall()
        return [dict(r) for r in rows]

    @_locked
    def rows(self, limit: int):
        """Newest-first full rows, for the owner's local log."""
        return self.db.execute("SELECT * FROM records ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()

    @_locked
    def reveal(self, seq: int) -> Optional[dict]:
        """Owner-only: the bundle a third party needs to check one commitment."""
        r = self.db.execute("SELECT * FROM records WHERE seq=?", (seq,)).fetchone()
        if r is None:
            return None
        payload = dict(seq=r["seq"], kind=r["kind"], ts=r["ts"], price=r["price"],
                       r=r["r"], rmode=r["rmode"], reason=r["reason"])
        if r["fill"] is not None:
            payload["fill"] = json.loads(r["fill"])
        # `canonical` = the exact bytes that were hashed, so non-Python verifiers (the web page) can
        # check it without re-serialising numbers (Python writes 74000.0, JavaScript writes 74000)
        return dict(payload=payload, canonical=canonical(payload), salt=r["salt"], hash=r["hash"],
                    chain=r["chain"], chain_ref=r["chain_ref"], revealed_at=time.time())
