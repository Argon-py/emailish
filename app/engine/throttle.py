"""Conservative SMTP rate limiting: global cap, per-MX-host cap, and a minimum
interval between connections to the same MX host."""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager


class Throttle:
    def __init__(self, global_concurrency: int = 8, per_host: int = 1, min_interval: float = 2.0):
        self.global_sem = asyncio.Semaphore(global_concurrency)
        self.per_host = per_host
        self.min_interval = min_interval
        self._host_sems: dict[str, asyncio.Semaphore] = {}
        self._next_ok: dict[str, float] = {}
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def slot(self, host: str):
        sem = self._host_sems.setdefault(host, asyncio.Semaphore(self.per_host))
        async with sem:
            async with self._lock:
                now = time.monotonic()
                start = max(now, self._next_ok.get(host, 0.0))
                self._next_ok[host] = start + self.min_interval
            if start > now:
                await asyncio.sleep(start - now)
            async with self.global_sem:
                yield
