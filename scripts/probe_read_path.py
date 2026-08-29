#!/usr/bin/env python
"""
Public read-path probe: the measurement behind PRODUCTION_READINESS items 1 and 2.

Item 1 measured this by hand. This is the same thing as a command, so "re-run
the probe to confirm" is repeatable rather than improvised, and so the numbers
before and after a change are produced the same way.

What it reports:

  latency      p50/p95 per endpoint, with `/ping` as the network + framework
               floor to subtract from the rest.
  cache        cold (X-Cache: MISS) against warm (HIT) for the same endpoint —
               the lever item 2 is about — plus the hit rate under the sweep.
  concurrency  p50 at 1/5/10/20 in flight. Item 1 found the points path flat to
               10 and 2x at 20 (queueing behind db_max_in_flight = 16), and the
               category path 3.4x the points path at 10.
  conditional  that an If-None-Match round trip is answered 304.

Public endpoints are rate limited to 100 req/min per IP, so the probe budgets
itself against that and sleeps rather than measuring its own 429s. That ceiling
is also why a single host cannot load-test past it: this measures latency under
concurrency, not capacity.

Usage:
    .venv/bin/python scripts/probe_read_path.py                        # localhost:8000
    .venv/bin/python scripts/probe_read_path.py --base-url https://api.courtvision.dev
    .venv/bin/python scripts/probe_read_path.py --samples 10 --sweep 1,5,10
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time
from dataclasses import dataclass, field

import httpx

# Public rate limit is 100 req/min per IP; stay under it with room to spare.
RATE_LIMIT_PER_MINUTE = 90

ENDPOINTS = {
    "ping": "/ping",
    "rankings (season, points)": "/v1/rankings/",
    "rankings (L14, points)": "/v1/rankings/?window=14",
    "rankings (season, categories)": "/v1/rankings/?format=categories",
}


@dataclass
class Budget:
    """Keeps the probe under the endpoint's own rate limit."""

    per_minute: int = RATE_LIMIT_PER_MINUTE
    sent: list[float] = field(default_factory=list)
    total: int = 0

    async def take(self) -> None:
        now = time.monotonic()
        self.sent = [t for t in self.sent if now - t < 60]
        if len(self.sent) >= self.per_minute:
            wait = 60 - (now - self.sent[0]) + 0.1
            print(f"  … rate-limit budget reached, sleeping {wait:.0f}s")
            await asyncio.sleep(wait)
            self.sent = []
        self.sent.append(time.monotonic())
        self.total += 1


@dataclass
class Sample:
    ms: float
    status: int
    cache: str
    bytes: int


async def _get(client: httpx.AsyncClient, path: str, budget: Budget, **kwargs) -> Sample:
    await budget.take()
    started = time.perf_counter()
    response = await client.get(path, **kwargs)
    elapsed_ms = (time.perf_counter() - started) * 1000
    return Sample(
        ms=elapsed_ms,
        status=response.status_code,
        cache=response.headers.get("x-cache", "-"),
        bytes=len(response.content),
    )


def _percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round(pct / 100 * len(ordered)) - 1))
    return ordered[index]


def _summarize(label: str, samples: list[Sample]) -> None:
    if not samples:
        print(f"  {label:<34} no samples")
        return
    latencies = [s.ms for s in samples]
    statuses = sorted({s.status for s in samples})
    hits = sum(1 for s in samples if s.cache == "HIT")
    cache = f"{hits}/{len(samples)} hit" if any(s.cache != "-" for s in samples) else "-"
    size_kb = statistics.median(s.bytes for s in samples) / 1024
    print(
        f"  {label:<34} p50 {_percentile(latencies, 50):7.1f} ms   "
        f"p95 {_percentile(latencies, 95):7.1f} ms   "
        f"{size_kb:6.1f} KB   cache {cache:<10} status {statuses}"
    )


async def measure_latency(client: httpx.AsyncClient, budget: Budget, samples: int) -> None:
    print(f"\nLatency, {samples} sequential samples each after one priming request")
    for label, path in ENDPOINTS.items():
        # The priming request is this process's first for the key, so on a
        # freshly started server it is also the cold one worth seeing.
        first = await _get(client, path, budget)
        results = [await _get(client, path, budget) for _ in range(samples)]
        _summarize(label, results)
        if first.cache == "MISS":
            print(f"  {'  (its priming request, cold)':<34} {first.ms:7.1f} ms")


async def measure_cache(client: httpx.AsyncClient, budget: Budget, samples: int) -> None:
    """Cold vs warm for the same work: a category render, then the cached bytes.

    Each cold sample uses a min_games value the cache has not seen, which is a
    genuine miss doing the full query, scoring and serialisation.
    """
    print(f"\nCold (X-Cache: MISS) against warm (HIT), {samples} samples each")
    cold: list[Sample] = []
    warm: list[Sample] = []
    # Offset the min_games values per run so a second probe within the cache TTL
    # still gets genuine misses rather than the previous run's entries.
    offset = int(time.time()) % 60
    for i in range(samples):
        path = f"/v1/rankings/?format=categories&min_games={1 + (offset + i) % 60}"
        cold.append(await _get(client, path, budget))
        warm.append(await _get(client, path, budget))

    _summarize("categories, cold", cold)
    _summarize("categories, warm", warm)
    if cold and warm:
        cold_p50, warm_p50 = _percentile([s.ms for s in cold], 50), _percentile([s.ms for s in warm], 50)
        saved = cold_p50 - warm_p50
        print(f"  {'':<34} a hit saves {saved:.1f} ms ({saved / cold_p50 * 100:.0f}% of the cold request)")
    unexpected = [s.cache for s in cold if s.cache == "HIT"]
    if unexpected:
        print("  NOTE: a 'cold' sample was served from cache — another client may share this key")


async def measure_concurrency(client: httpx.AsyncClient, budget: Budget, levels: list[int]) -> None:
    print("\nConcurrency, p50 per request at each level")
    for label, path in (("points", "/v1/rankings/"), ("categories", "/v1/rankings/?format=categories")):
        for level in levels:
            await _get(client, path, budget)                 # warm the entry first
            results = await asyncio.gather(*(_get(client, path, budget) for _ in range(level)))
            _summarize(f"{label} @ {level} in flight", list(results))


async def check_conditional(client: httpx.AsyncClient, budget: Budget) -> None:
    print("\nConditional request")
    await budget.take()
    first = await client.get("/v1/rankings/")
    etag = first.headers.get("etag")
    if not etag:
        print("  no ETag on the response — caching is off, or an older build is deployed")
        return
    await budget.take()
    second = await client.get("/v1/rankings/", headers={"If-None-Match": etag})
    verdict = "304, empty body" if second.status_code == 304 and not second.content else f"{second.status_code}"
    print(f"  ETag {etag} -> {verdict}")
    print(f"  Cache-Control: {first.headers.get('cache-control', '-')}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--samples", type=int, default=8, help="sequential samples per measurement")
    parser.add_argument("--sweep", default="1,5,10,20", help="concurrency levels, comma separated")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--rate-limit", type=int, default=RATE_LIMIT_PER_MINUTE,
                        help="requests per minute to stay under; 0 removes the budget — the server still\n                             limits public routes to 100/min per IP, so expect 429s")
    args = parser.parse_args()

    levels = [int(x) for x in args.sweep.split(",") if x.strip()]
    budget = Budget(per_minute=args.rate_limit or 10**9)

    print(f"Probing {args.base_url}")
    async with httpx.AsyncClient(base_url=args.base_url, timeout=args.timeout) as client:
        await measure_latency(client, budget, args.samples)
        await measure_cache(client, budget, args.samples)
        await measure_concurrency(client, budget, levels)
        await check_conditional(client, budget)

    print(f"\n{budget.total} requests sent.")


if __name__ == "__main__":
    asyncio.run(main())
