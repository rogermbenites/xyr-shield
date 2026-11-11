"""Where commitment hashes are published.

local  — append-only JSONL hash chain in data/ (works offline, for dev/demo)
near   — the ShieldLog contract on NEAR, via chain/near.mjs (near-api-js)

Both return records shaped {n, ts, ref}.
"""
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone

from . import DATA, ROOT


class LocalChain:
    name = "local"

    def __init__(self, path=DATA / "local_chain.jsonl"):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch()

    def _rows(self):
        with open(self.path) as f:
            return [json.loads(l) for l in f if l.strip()]

    def commit(self, h: str) -> dict:
        rows = self._rows()
        if any(r["hash"] == h for r in rows):
            raise ValueError("duplicate commitment")
        prev = rows[-1]["link"] if rows else "0" * 64
        rec = dict(n=len(rows), hash=h, ts=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   link=hashlib.sha256(f"{prev}:{h}".encode()).hexdigest())
        with open(self.path, "a") as f:
            f.write(json.dumps(rec) + "\n")
        return dict(n=rec["n"], ts=rec["ts"], ref=rec["link"])

    def get(self, h: str):
        for r in self._rows():
            if r["hash"] == h:
                return dict(n=r["n"], ts=r["ts"], ref=r["link"])
        return None

    def count(self) -> int:
        return len(self._rows())


class NearChain:
    name = "near"
    script = ROOT / "chain" / "near.mjs"

    def _run(self, *args) -> dict:
        out = subprocess.run(["node", str(self.script), *args], capture_output=True, text=True,
                             timeout=90, cwd=str(ROOT / "chain"), env=os.environ.copy())
        if out.returncode != 0:
            raise RuntimeError(f"near.mjs {args[0]} failed: {out.stderr.strip()[-500:]}")
        return json.loads(out.stdout.strip().splitlines()[-1])

    def commit(self, h: str) -> dict:
        return self._run("commit", h)

    def get(self, h: str):
        rec = self._run("get", h)
        return rec or None

    def count(self) -> int:
        return int(self._run("count")["count"])


def get_chain():
    return NearChain() if os.environ.get("SHIELD_CHAIN", "local") == "near" else LocalChain()
