"""Per-hop delegation cost: chained RFC 8693 exchanges at depth 1..5.

Two CNS reviewers asked for a delegation-depth sweep charting "the
compounding computational and payload inflation overheads introduced as the
act claim structure grows". This measures both halves directly:

  computational -- wall-clock cost of each successive exchange hop, and the
                   cumulative cost of establishing a depth-d chain.
  payload       -- the serialized size of the access token after each hop.

Depth d means d successive token exchanges after the initial
client_credentials grant, each taking the previous token as subject_token
and carrying the previous token as actor_token.

Usage:  python -m benchmark.depth_sweep [--reps 30] [--max-depth 5]
Writes results/processed/depth_sweep.json
"""
import argparse
import base64
import json
import statistics as st
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from agent.config import (API_ENDPOINT, CLIENT_ID, CLIENT_SECRET,
                          TARGET_AUDIENCE, TOKEN_ENDPOINT)
from agent.dpop import generate_dpop_proof, generate_keypair

EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
ATT = "urn:ietf:params:oauth:token-type:access_token"
OUT = Path(__file__).parent.parent / "results" / "processed" / "depth_sweep.json"


def _claims(tok: str) -> Dict[str, Any]:
    p = tok.split(".")[1]
    p += "=" * (-len(p) % 4)
    return json.loads(base64.urlsafe_b64decode(p))


def _mint(client: httpx.Client, dpop: Optional[str]) -> tuple:
    h = {"DPoP": dpop} if dpop else {}
    t0 = time.perf_counter()
    r = client.post(TOKEN_ENDPOINT, data={
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}, headers=h)
    ms = (time.perf_counter() - t0) * 1000
    r.raise_for_status()
    return r.json()["access_token"], ms


def _exchange(client: httpx.Client, subject: str, actor: Optional[str],
              dpop: Optional[str]) -> tuple:
    data = {
        "grant_type": EXCHANGE, "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "subject_token": subject, "subject_token_type": ATT,
        "audience": TARGET_AUDIENCE, "requested_token_type": ATT,
    }
    if actor:
        data["actor_token"] = actor
        data["actor_token_type"] = ATT
    h = {"DPoP": dpop} if dpop else {}
    t0 = time.perf_counter()
    r = client.post(TOKEN_ENDPOINT, data=data, headers=h)
    ms = (time.perf_counter() - t0) * 1000
    return r, ms


def run_chain(client: httpx.Client, depth: int, mode: str) -> Optional[Dict]:
    """Build a depth-`depth` delegation chain; return per-hop timings/sizes."""
    priv = pub = None
    if mode == "C":
        priv, pub = generate_keypair()

    def proof(url):
        return generate_dpop_proof(priv, pub, "POST", url) if mode == "C" else None

    token, mint_ms = _mint(client, proof(TOKEN_ENDPOINT))
    hops, sizes, acts = [], [len(token)], []
    prev = None
    for _ in range(depth):
        r, ms = _exchange(client, token, prev, proof(TOKEN_ENDPOINT))
        if r.status_code != 200:
            return {"failed_at_hop": len(hops) + 1, "status": r.status_code,
                    "body": r.text[:200]}
        prev, token = token, r.json()["access_token"]
        hops.append(ms)
        sizes.append(len(token))
        acts.append("act" in _claims(token))

    # final resource call with the leaf token
    h = {"Authorization": f"Bearer {token}"}
    if mode == "C":
        h["DPoP"] = generate_dpop_proof(priv, pub, "POST", API_ENDPOINT,
                                        access_token=token)
    t0 = time.perf_counter()
    ra = client.post(API_ENDPOINT, headers=h, json={})
    api_ms = (time.perf_counter() - t0) * 1000
    return {"mint_ms": mint_ms, "hop_ms": hops, "chain_ms": sum(hops),
            "token_bytes": sizes, "act_present": acts,
            "api_ms": api_ms, "api_status": ra.status_code}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=30)
    ap.add_argument("--max-depth", type=int, default=5)
    args = ap.parse_args()

    results: Dict[str, Any] = {"reps": args.reps, "max_depth": args.max_depth,
                               "by_mode": {}}
    with httpx.Client(timeout=30.0) as client:
        for mode in ("B", "C"):
            per_depth = {}
            for d in range(1, args.max_depth + 1):
                runs = [run_chain(client, d, mode) for _ in range(args.reps)]
                bad = [r for r in runs if r is None or "failed_at_hop" in r]
                good = [r for r in runs if r and "failed_at_hop" not in r]
                if not good:
                    per_depth[d] = {"all_failed": True,
                                    "example": bad[0] if bad else None}
                    print(f"  mode {mode} depth {d}: ALL FAILED -> {bad[0] if bad else ''}")
                    continue
                chain = [r["chain_ms"] for r in good]
                api = [r["api_ms"] for r in good]
                last_hop = [r["hop_ms"][-1] for r in good]
                sizes = good[0]["token_bytes"]
                per_depth[d] = {
                    "n": len(good), "n_failed": len(bad),
                    "chain_ms_median": round(st.median(chain), 2),
                    "chain_ms_p95": round(sorted(chain)[int(0.95 * len(chain)) - 1], 2),
                    "last_hop_ms_median": round(st.median(last_hop), 2),
                    "api_ms_median": round(st.median(api), 2),
                    "token_bytes_by_hop": sizes,
                    "act_present_any": any(good[0]["act_present"]),
                }
                print(f"  mode {mode} depth {d}: chain={per_depth[d]['chain_ms_median']:.2f} ms "
                      f"(last hop {per_depth[d]['last_hop_ms_median']:.2f}) "
                      f"api={per_depth[d]['api_ms_median']:.2f} ms "
                      f"tokens={sizes} act={per_depth[d]['act_present_any']} "
                      f"failed={len(bad)}")
            results["by_mode"][mode] = per_depth
    OUT.parent.mkdir(parents=True, exist_ok=True)
    json.dump(results, open(OUT, "w"), indent=1)
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
