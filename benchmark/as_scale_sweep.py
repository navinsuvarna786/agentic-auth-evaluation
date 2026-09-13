"""Locate the authorization-server ceiling with 1 vs N Keycloak instances.

A CNS reviewer asked whether multi-instance Keycloak would locate the true
protocol-level rejection cliff. Our single-instance measurement capped at
880-917 achieved RPS with the agent-side token-exchange leg rising from ~4 ms
to ~87 ms median, which identified the authorization server rather than the
verifier as the binding constraint at that scale.

This driver replays that load while distributing token and exchange requests
round-robin across one or more Keycloak instances that share a database and
issue an identical `iss`. Comparing achieved throughput and exchange latency
at matched target rates isolates whether the ceiling is the authorization
server's per-instance capacity.

Each worker process paces its own share of the target rate. The verifier is
excluded from the loop on purpose: we drive only the token and exchange
endpoints, so the measurement is of authorization-server capacity alone.

Usage:
  python -m benchmark.as_scale_sweep --as-urls URL[,URL...] \
      --rates 512,1024,1536 --workers 8 --duration 20 --tag 3as
"""
import argparse
import json
import multiprocessing as mp
import statistics as st
import time
from pathlib import Path
from typing import Dict, List

import httpx

from agent.config import CLIENT_ID, CLIENT_SECRET, TARGET_AUDIENCE
from agent.dpop import generate_dpop_proof, generate_keypair

EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
ATT = "urn:ietf:params:oauth:token-type:access_token"
OUT = Path(__file__).parent.parent / "results" / "processed"


def worker(wid: int, as_urls: List[str], rate: float, duration: float, q) -> None:
    """One process; paces `rate` req/s of mint+exchange across as_urls."""
    priv, pub = generate_keypair()
    gap = 1.0 / rate
    mint_ms, exch_ms, errors = [], [], 0
    start = time.perf_counter()
    n = 0
    with httpx.Client(timeout=20.0) as c:
        while True:
            now = time.perf_counter()
            if now - start >= duration:
                break
            target = start + n * gap
            if target > now:
                time.sleep(target - now)
            url = as_urls[(wid + n) % len(as_urls)]
            token_ep = f"{url}/realms/ephemeral-agents/protocol/openid-connect/token"
            try:
                t0 = time.perf_counter()
                r = c.post(token_ep, data={
                    "grant_type": "client_credentials",
                    "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
                    headers={"DPoP": generate_dpop_proof(priv, pub, "POST", token_ep)})
                m = (time.perf_counter() - t0) * 1000
                if r.status_code != 200:
                    errors += 1
                    n += 1
                    continue
                sub = r.json()["access_token"]
                mint_ms.append(m)

                t1 = time.perf_counter()
                r2 = c.post(token_ep, data={
                    "grant_type": EXCHANGE, "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET, "subject_token": sub,
                    "subject_token_type": ATT, "audience": TARGET_AUDIENCE,
                    "requested_token_type": ATT},
                    headers={"DPoP": generate_dpop_proof(priv, pub, "POST", token_ep)})
                e = (time.perf_counter() - t1) * 1000
                if r2.status_code != 200:
                    errors += 1
                else:
                    exch_ms.append(e)
            except Exception:
                errors += 1
            n += 1
    elapsed = time.perf_counter() - start
    q.put({"wid": wid, "attempted": n, "ok": len(exch_ms), "errors": errors,
           "elapsed": elapsed, "mint_ms": mint_ms, "exch_ms": exch_ms})


def pct(xs, p):
    return round(sorted(xs)[max(0, int(p * len(xs)) - 1)], 2) if xs else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-urls", required=True)
    ap.add_argument("--rates", default="512,1024,1536")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--duration", type=float, default=20.0)
    ap.add_argument("--tag", default="run")
    args = ap.parse_args()

    as_urls = [u.strip().rstrip("/") for u in args.as_urls.split(",")]
    rates = [int(x) for x in args.rates.split(",")]
    out: Dict = {"tag": args.tag, "as_instances": len(as_urls),
                 "as_urls": as_urls, "workers": args.workers,
                 "duration_s": args.duration, "by_rate": {}}
    print(f"AS instances: {len(as_urls)}  workers: {args.workers}")
    for rate in rates:
        per = rate / args.workers
        q = mp.Queue()
        ps = [mp.Process(target=worker, args=(i, as_urls, per, args.duration, q))
              for i in range(args.workers)]
        [p.start() for p in ps]
        res = [q.get() for _ in ps]
        [p.join() for p in ps]
        ok = sum(r["ok"] for r in res)
        err = sum(r["errors"] for r in res)
        att = sum(r["attempted"] for r in res)
        el = max(r["elapsed"] for r in res)
        exch = [x for r in res for x in r["exch_ms"]]
        mint = [x for r in res for x in r["mint_ms"]]
        row = {"target_rps": rate, "achieved_rps": round(ok / el, 1),
               "attempted": att, "ok": ok, "errors": err,
               "exch_ms_median": pct(exch, 0.5), "exch_ms_p95": pct(exch, 0.95),
               "mint_ms_median": pct(mint, 0.5), "mint_ms_p95": pct(mint, 0.95)}
        out["by_rate"][rate] = row
        print(f"  target {rate:>5} -> achieved {row['achieved_rps']:>7.1f} rps  "
              f"exch med {row['exch_ms_median']} p95 {row['exch_ms_p95']}  "
              f"mint med {row['mint_ms_median']}  errors {err}")
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / f"as_scale_{args.tag}.json"
    json.dump(out, open(p, "w"), indent=1)
    print(f"wrote {p}")


if __name__ == "__main__":
    main()
