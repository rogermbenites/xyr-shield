"""R oracle plug-ins: the model (how R is computed) and the bar source (what it is computed on).

Neither is part of the proof — the oracle only chains and anchors whatever values the model
emits — so a production model lives in a PRIVATE module and is loaded by name:

    RNODE_MODEL=my_private_pkg.r:RealR     RNODE_SOURCE=my_private_pkg.bars:TickBars

The defaults below are public stand-ins so the oracle runs end to end out of the box.

Model interface:   update(close) -> (r, mode)    peek(close) -> r     warmup_bars: int
Source interface:  warmup() -> [bar]   async stream(on_close, on_tick)   progress() -> 0..1
                   bar = dict(end_ms, o, h, l, c)
"""
import asyncio
import copy
import importlib
import json
import os
import time

import requests


def load(env: str, default):
    spec = os.environ.get(env)
    if not spec:
        return default()
    mod, _, cls = spec.partition(":")
    return getattr(importlib.import_module(mod), cls)()


class DemoR:
    """Stand-in model: Wilder RSI(14) mapped to -1..+1; mode follows its direction."""
    name = "demo-rsi"
    warmup_bars = 50

    def __init__(self, n: int = 14):
        self.n, self.prev_c, self.up, self.dn, self.k, self.r = n, None, 0.0, 0.0, 0, None

    def update(self, close: float):
        if self.prev_c is not None:
            d = close - self.prev_c
            a = 1 / self.n
            self.up = (1 - a) * self.up + a * max(d, 0)
            self.dn = (1 - a) * self.dn + a * max(-d, 0)
        self.prev_c, self.k = close, self.k + 1
        rsi = 50.0 if self.up + self.dn == 0 else 100 * self.up / (self.up + self.dn)
        r = (rsi - 50) / 50
        mode = "HOLD" if self.r is None or r == self.r else ("LONG" if r > self.r else "SHORT")
        self.r = r
        return r, mode

    def peek(self, close: float):
        return copy.deepcopy(self).update(close)[0]


class BinanceKlines:
    """Stand-in source: BTCUSD_PERP 1-minute candles from Binance coin-M (public REST + WS)."""
    REST = "https://dapi.binance.com/dapi/v1/klines"
    WS = "wss://dstream.binance.com/ws/btcusd_perp@kline_1m"

    def __init__(self):
        self.price, self._start, self._end = None, None, None

    def warmup(self):
        rows = requests.get(self.REST, params=dict(symbol="BTCUSD_PERP", interval="1m", limit=600),
                            timeout=20).json()
        now = time.time() * 1000
        return [dict(end_ms=int(k[6]), o=float(k[1]), h=float(k[2]), l=float(k[3]), c=float(k[4]))
                for k in rows if k[6] < now]          # closed candles only

    async def stream(self, on_close, on_tick):
        import websockets
        async with websockets.connect(self.WS, ping_interval=15, ping_timeout=20) as ws:
            async for raw in ws:
                k = json.loads(raw).get("k")
                if not k:
                    continue
                self.price, self._start, self._end = float(k["c"]), k["t"], k["T"]
                if k["x"]:
                    on_close(dict(end_ms=int(k["T"]), o=float(k["o"]), h=float(k["h"]),
                                  l=float(k["l"]), c=float(k["c"])))
                else:
                    on_tick(float(k["c"]))

    def progress(self):
        if self._start is None:
            return 0.0
        return max(0.0, min(1.0, (time.time() * 1000 - self._start) / (self._end - self._start)))
