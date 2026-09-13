"""Version-compatibility probe: does Keycloak 26.6 change what the study's
security scenarios observe at the exchange endpoint?

Runs S4 (actor_token / act) and S6 (exchange-mediated laundering) against a
throwaway 26.6 instance and persists every observation to
results/processed/kc266_compat.json so the paper's version-scoping is a
measured row rather than a release-notes citation.

The instance is standalone (docker run, own realm, loopback only, tunnelled
to 127.0.0.1:18091); nothing here touches the 26.2 study stack.

Reuses the realm layout of probe_exchange_version_compare.py.
"""
import base64
import json
import os
import sys
import time

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent.dpop import generate_dpop_proof, generate_keypair  # noqa: E402

KC = os.environ.get("KC_PROBE_URL", "http://localhost:18091")
REALM = "s6probe"
ADMIN_PW = os.environ.get("KC_PROBE_ADMIN_PW", "s6probe-throwaway-pw")
CID = "probe-client"
CSEC = "probe-secret"
AUD = "probe-api"
TOKEN_EP = f"{KC}/realms/{REALM}/protocol/openid-connect/token"
EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
ATT = "urn:ietf:params:oauth:token-type:access_token"
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "results", "processed", "kc266_compat.json")


def claims(tok):
    p = tok.split(".")[1]
    p += "=" * (-len(p) % 4)
    return json.loads(base64.urlsafe_b64decode(p))


def admin_headers():
    r = httpx.post(f"{KC}/realms/master/protocol/openid-connect/token", data={
        "grant_type": "password", "client_id": "admin-cli",
        "username": "admin", "password": ADMIN_PW}, timeout=15)
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}",
            "Content-Type": "application/json"}


def server_version(h):
    r = httpx.get(f"{KC}/admin/serverinfo", headers=h, timeout=15)
    r.raise_for_status()
    return r.json().get("systemInfo", {}).get("version", "unknown")


def setup(h):
    base = f"{KC}/admin/realms"
    if httpx.get(f"{base}/{REALM}", headers=h, timeout=15).status_code == 200:
        httpx.delete(f"{base}/{REALM}", headers=h, timeout=30)
    httpx.post(base, headers=h, json={"realm": REALM, "enabled": True}, timeout=30).raise_for_status()
    httpx.post(f"{base}/{REALM}/clients", headers=h, json={
        "clientId": AUD, "enabled": True, "publicClient": False,
        "serviceAccountsEnabled": False, "standardFlowEnabled": False,
    }, timeout=30).raise_for_status()
    httpx.post(f"{base}/{REALM}/clients", headers=h, json={
        "clientId": CID, "enabled": True, "publicClient": False,
        "secret": CSEC, "serviceAccountsEnabled": True,
        "standardFlowEnabled": False, "directAccessGrantsEnabled": False,
        "attributes": {"standard.token.exchange.enabled": "true",
                       "dpop.bound.access.tokens": "false"},
    }, timeout=30).raise_for_status()
    cid = httpx.get(f"{base}/{REALM}/clients", headers=h,
                    params={"clientId": CID}, timeout=15).json()[0]["id"]
    httpx.post(f"{base}/{REALM}/clients/{cid}/protocol-mappers/models", headers=h, json={
        "name": "aud-mapper", "protocol": "openid-connect",
        "protocolMapper": "oidc-audience-mapper",
        "config": {"included.client.audience": AUD, "access.token.claim": "true"},
    }, timeout=15).raise_for_status()


def mint(dpop=None):
    h = {"DPoP": dpop} if dpop else {}
    return httpx.post(TOKEN_EP, data={
        "grant_type": "client_credentials", "client_id": CID, "client_secret": CSEC,
    }, headers=h, timeout=15)


def exchange(subject, dpop=None, actor=None, client_id=CID, client_secret=CSEC):
    h = {"DPoP": dpop} if dpop else {}
    data = {"grant_type": EXCHANGE, "subject_token": subject,
            "subject_token_type": ATT, "audience": AUD, "requested_token_type": ATT}
    if client_id is not None:
        data["client_id"] = client_id
    if client_secret is not None:
        data["client_secret"] = client_secret
    if actor is not None:
        data["actor_token"] = actor
        data["actor_token_type"] = ATT
    return httpx.post(TOKEN_EP, data=data, headers=h, timeout=15)


def jkt(tok):
    return (claims(tok).get("cnf") or {}).get("jkt")


def err(r):
    try:
        j = r.json()
        return j.get("error"), j.get("error_description")
    except Exception:
        return None, r.text[:200]


rows = []


def record(scenario, attack, r, **extra):
    e, d = err(r)
    row = {"scenario": scenario, "attack": attack, "status": r.status_code,
           "error": e, "error_description": d, **extra}
    rows.append(row)
    print(f"  {scenario:<4} {attack:<34} -> {r.status_code} {e or ''} {d or ''}"
          + (f"  {extra}" if extra else ""))
    return row


def main():
    h = admin_headers()
    version = server_version(h)
    print(f"Keycloak {version} at {KC}")
    setup(h)

    print("\n-- baseline --")
    plain = mint()
    assert plain.status_code == 200, plain.text
    pt = plain.json()["access_token"]
    r = exchange(pt)
    record("base", "plain_exchange", r,
           aud=claims(r.json()["access_token"]).get("aud") if r.status_code == 200 else None)

    print("\n-- S6: exchange-mediated laundering --")
    r = exchange(pt, client_id=None, client_secret=None)
    record("S6a", "stolen_token_no_client_creds", r)
    r = exchange(pt)
    record("S6b", "launder_bearer_subject", r)

    vpriv, vpub = generate_keypair()
    rv = mint(generate_dpop_proof(vpriv, vpub, "POST", TOKEN_EP))
    assert rv.status_code == 200, rv.text
    vt = rv.json()["access_token"]
    vjkt = jkt(vt)
    assert vjkt, "victim token not sender-constrained; is --features=dpop on?"

    apriv, apub = generate_keypair()
    r = exchange(vt, generate_dpop_proof(apriv, apub, "POST", TOKEN_EP))
    record("S6c", "rebind_dpop_subject_attacker_key", r,
           rebound=(r.status_code == 200 and jkt(r.json()["access_token"]) not in (None, vjkt)))
    r = exchange(vt, generate_dpop_proof(vpriv, vpub, "POST", TOKEN_EP))
    record("S6c", "control_dpop_subject_victim_key", r,
           jkt_preserved=(r.status_code == 200 and jkt(r.json()["access_token"]) == vjkt))
    # Mode C construction available on this version: bearer subject + DPoP at the exchange
    cpriv, cpub = generate_keypair()
    r = exchange(pt, generate_dpop_proof(cpriv, cpub, "POST", TOKEN_EP))
    record("S6", "modeC_bearer_subject_plus_dpop", r,
           result_sender_constrained=(r.status_code == 200 and jkt(r.json()["access_token"]) is not None))

    print("\n-- S4: actor_token / act claim --")
    actor_probes = [
        ("actor_token_valid", mint().json()["access_token"]),
        ("actor_token_garbage", "not-a-jwt-at-all"),
        ("actor_token_forged_jwt", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhdHRhY2tlciJ9.xxxxx"),
    ]
    for attack, actor in actor_probes:
        r = exchange(pt, actor=actor)
        act = None
        if r.status_code == 200:
            act = claims(r.json()["access_token"]).get("act")
        record("S4", attack, r, act_claim_present=act is not None, act_claim=act)
    # depth-2: exchange the exchanged token, carrying the first as actor
    first = exchange(pt)
    if first.status_code == 200:
        ft = first.json()["access_token"]
        r = exchange(ft, actor=ft)
        act = claims(r.json()["access_token"]).get("act") if r.status_code == 200 else None
        record("S4", "depth2_chain_actor_is_previous", r,
               act_claim_present=act is not None, act_claim=act)

    out = {"metadata": {"timestamp": time.time(),
                        "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "keycloak_version": version, "keycloak_url": KC,
                        "realm": REALM, "features": ["dpop"],
                        "mode": "standalone throwaway instance (docker run, start-dev)"},
           "rows": rows}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nwrote {OUT} ({len(rows)} rows)")


if __name__ == "__main__":
    main()
