"""Re-derive candidate disposable-mail MX operators (see app/data/disposable_mx_operators.txt).

Resolves MX (DNS-over-HTTPS) for a random sample of listed disposable domains and ranks
the MX operators (registrable domain of the MX host) by how many of them they serve.
Output is a candidate ranking for MANUAL review: shared infrastructure (Cloudflare,
Google, Mailgun, registrars, parking services...) must be excluded by a human before
anything is added to the data file.

  python scripts/derive_disposable_mx.py --sample 900
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import random
from pathlib import Path

import httpx

DATA = Path(__file__).resolve().parent.parent / "app" / "data"


def registrable(host: str) -> str:
    p = host.split(".")
    if len(p) > 2 and p[-2] in ("co", "com", "net", "org") and len(p[-1]) == 2:
        return ".".join(p[-3:])
    return ".".join(p[-2:])


async def mx(client, sem, domain):
    async with sem:
        try:
            r = await client.get("https://cloudflare-dns.com/dns-query", params={"name": domain, "type": "MX"},
                                 headers={"accept": "application/dns-json"}, timeout=10)
            return domain, [a["data"].split()[-1].rstrip(".").lower()
                            for a in r.json().get("Answer", []) if a.get("type") == 15]
        except Exception:
            return domain, []


async def main(sample: int, seed: int):
    doms = [l.strip() for l in (DATA / "disposable_domains.txt").read_text().splitlines()
            if l.strip() and not l.startswith("#")]
    random.seed(seed)
    pick = random.sample(doms, min(sample, len(doms)))
    sem = asyncio.Semaphore(10)
    async with httpx.AsyncClient() as c:
        res = await asyncio.gather(*(mx(c, sem, d) for d in pick))
    disposable = set(doms)
    count, examples = collections.Counter(), collections.defaultdict(list)
    for d, hosts in res:
        for r in {registrable(h) for h in hosts if h}:
            count[r] += 1
            examples[r].append(d)
    print(f"{sum(1 for _, h in res if h)} of {len(pick)} sampled domains returned MX records\n")
    print("served\toperator\tself-listed\texamples")
    for r, n in count.most_common(50):
        print(f"{n}\t{r}\t{'yes' if r in disposable else ''}\t{', '.join(examples[r][:3])}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=900)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    asyncio.run(main(a.sample, a.seed))
