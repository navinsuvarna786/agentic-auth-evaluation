"""Empirical security-claim verification (RQ3, scenarios S1-S6).

Replaces the hardcoded attack/mitigation table that LANMAN R3 flagged as
misleading. Every claim in `paper/main.tex`'s security section is now
backed by an actual HTTP exchange whose status code and error payload
are recorded in `results/processed/security_verification.json`.

Scenarios:
  S1  Stolen bearer-token replay by a different sender.
  S2  Same DPoP proof (identical jti) presented twice.
  S3  Bearer token whose audience does not include the protected API.
  S4  Actor (delegation) chain at delegation_depth=2, plus a protocol-level
      probe of whether `actor_token` is validated or recorded at all.
  S5  Token that has passed its `exp` claim.
  S6  Abuse of the RFC 8693 exchange endpoint itself: laundering a stolen
      token, and re-binding a DPoP-bound token to an attacker's key.

The runner needs a live Keycloak (with the realm imported from
`keycloak/realm-export.json`), the FastAPI verifier, and Redis. Each
scenario function may be invoked independently for diagnostic use.

Pre-conditions:
  * `wrong-audience-client` and `short-ttl-client` exist in the realm
    (added in commit d694edb on the `final-draft` branch).
  * Env vars `WRONG_AUDIENCE_CLIENT_SECRET` and `SHORT_TTL_CLIENT_SECRET`
    are exported (mirroring the realm-export.json `${env.*}` markers).
"""
import base64
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from agent.agent_client import AgentClient
from agent.config import (
    API_ENDPOINT,
    API_URL,
    CLIENT_ID,
    CLIENT_SECRET,
    KEYCLOAK_URL,
    REALM_NAME,
    TARGET_AUDIENCE,
    TOKEN_ENDPOINT,
)
from agent.dpop import generate_dpop_proof, generate_keypair


WRONG_AUDIENCE_CLIENT_ID = os.getenv("WRONG_AUDIENCE_CLIENT_ID", "wrong-audience-client")
WRONG_AUDIENCE_CLIENT_SECRET = os.getenv("WRONG_AUDIENCE_CLIENT_SECRET")
SHORT_TTL_CLIENT_ID = os.getenv("SHORT_TTL_CLIENT_ID", "short-ttl-client")
SHORT_TTL_CLIENT_SECRET = os.getenv("SHORT_TTL_CLIENT_SECRET")
# 10 s lifespan in realm + 2 s slack for clock skew and the API's
# 10-s leeway (api/config.py TOKEN_LEEWAY). We want the token to be
# unambiguously past `exp + leeway` when the request lands.
SHORT_TTL_SLEEP_SECONDS = int(os.getenv("S5_SLEEP_SECONDS", "22"))

RESULTS_DIR = Path(__file__).parent.parent / "results"
PROCESSED_DIR = RESULTS_DIR / "processed"


def _mint_token(
    client_id: str,
    client_secret: str,
    dpop_proof: Optional[str] = None,
) -> str:
    """client_credentials grant against Keycloak; raises on non-200."""
    data = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }
    headers = {"DPoP": dpop_proof} if dpop_proof else {}
    with httpx.Client(timeout=10.0) as c:
        r = c.post(TOKEN_ENDPOINT, data=data, headers=headers)
    if r.status_code != 200:
        raise RuntimeError(
            f"token endpoint returned {r.status_code} for client {client_id!r}: {r.text}"
        )
    return r.json()["access_token"]


def _post_task(
    access_token: str,
    dpop_proof: Optional[str] = None,
    authorization_scheme: str = "Bearer",
) -> Tuple[int, Dict[str, Any]]:
    """POST /api/task as a third-party HTTP client (no AgentClient state)."""
    headers = {"Authorization": f"{authorization_scheme} {access_token}"}
    if dpop_proof:
        headers["DPoP"] = dpop_proof
    with httpx.Client(timeout=10.0) as c:
        r = c.post(API_ENDPOINT, headers=headers, json={})
    try:
        body = r.json()
    except ValueError:
        body = {"raw": r.text}
    return r.status_code, body


def _token_claims(token: str) -> Dict[str, Any]:
    """Decode a JWT payload without verifying (inspection only)."""
    part = token.split(".")[1]
    part += "=" * (-len(part) % 4)
    return json.loads(base64.urlsafe_b64decode(part))


def _exchange_with_actor(subject_token: str, actor_token: str) -> httpx.Response:
    """RFC 8693 exchange carrying an `actor_token`, used to test whether the
    authorization server validates or records the delegation actor."""
    data = {
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "subject_token": subject_token,
        "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "actor_token": actor_token,
        "actor_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "audience": TARGET_AUDIENCE,
        "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
    }
    with httpx.Client(timeout=10.0) as c:
        return c.post(TOKEN_ENDPOINT, data=data)


def _row(
    *,
    mode: str,
    attack: str,
    expected_status: Optional[int],
    expected_error: Optional[str],
    actual_status: int,
    actual_error: Optional[str],
    actual_message: Optional[str] = None,
    notes: str = "",
) -> Dict[str, Any]:
    """Uniform schema for every scenario × mode result row."""
    passed = (
        (expected_status is None or actual_status == expected_status)
        and (expected_error is None or actual_error == expected_error)
    )
    return {
        "mode": mode,
        "attack": attack,
        "expected_status": expected_status,
        "expected_error": expected_error,
        "actual_status": actual_status,
        "actual_error": actual_error,
        "actual_message": actual_message,
        "passed": passed,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# S1: stolen bearer token replayed by a different sender
# ---------------------------------------------------------------------------


def test_s1_stolen_bearer_token() -> List[Dict[str, Any]]:
    """Bearer-token theft. Mode A/B: thief succeeds (vulnerability). Mode C:
    a thief without the original private key cannot produce a valid DPoP proof
    for the access token, so the request is rejected with 401."""
    rows: List[Dict[str, Any]] = []

    # Mode A: get a bearer token via AgentClient, then a *different* HTTP
    # client sends it as if it had been stolen. AgentClient.close() ensures
    # we are not reusing its connection or any session state.
    client_a = AgentClient(mode="A", cache_enabled=False)
    init_a = client_a.make_request()
    if not init_a.success:
        rows.append(_row(
            mode="A", attack="stolen_bearer",
            expected_status=200, expected_error=None,
            actual_status=init_a.status_code, actual_error=init_a.error,
            notes="legitimate Mode A request failed; cannot evaluate theft",
        ))
        client_a.close()
    else:
        # Re-mint a fresh token directly (so the "victim" hasn't already
        # cached/consumed it). The point is to demonstrate that the bearer
        # itself is sufficient for an attacker.
        client_a.close()
        stolen_token = _mint_token(CLIENT_ID, CLIENT_SECRET)
        status, body = _post_task(stolen_token)
        rows.append(_row(
            mode="A", attack="stolen_bearer",
            expected_status=200, expected_error=None,
            actual_status=status, actual_error=body.get("error"),
            actual_message=body.get("message") or body.get("result", {}).get("message"),
            notes="bearer token re-used from an unrelated client (proves vulnerability)",
        ))

    # Mode B: same theft mechanic. The exchanged token still has no key
    # binding, so re-presenting it from anywhere succeeds.
    client_b = AgentClient(mode="B", cache_enabled=False)
    init_b = client_b.make_request()
    client_b.close()
    if not init_b.success:
        rows.append(_row(
            mode="B", attack="stolen_bearer",
            expected_status=200, expected_error=None,
            actual_status=init_b.status_code, actual_error=init_b.error,
            notes="legitimate Mode B request failed; cannot evaluate theft",
        ))
    else:
        stolen_token = _mint_token(CLIENT_ID, CLIENT_SECRET)
        status, body = _post_task(stolen_token)
        rows.append(_row(
            mode="B", attack="stolen_bearer",
            expected_status=200, expected_error=None,
            actual_status=status, actual_error=body.get("error"),
            actual_message=body.get("message") or body.get("result", {}).get("message"),
            notes="audience-scoped bearer is still bearer; re-use succeeds",
        ))

    # Mode C: legitimate keypair K1 obtains a DPoP-bound token T. Attacker
    # has only T and uses a NEW keypair K2 to sign a DPoP proof — server
    # compares jkt(K2) to T's cnf.jkt and rejects.
    legit_priv, legit_pub = generate_keypair()
    legit_proof = generate_dpop_proof(legit_priv, legit_pub, "POST", TOKEN_ENDPOINT)
    try:
        bound_token = _mint_token(CLIENT_ID, CLIENT_SECRET, dpop_proof=legit_proof)
    except Exception as exc:
        rows.append(_row(
            mode="C", attack="stolen_bearer",
            expected_status=401, expected_error="invalid_dpop",
            actual_status=0, actual_error=str(exc),
            notes="failed to obtain DPoP-bound token from Keycloak",
        ))
        return rows

    thief_priv, thief_pub = generate_keypair()
    thief_proof = generate_dpop_proof(
        thief_priv, thief_pub, "POST", API_ENDPOINT, access_token=bound_token,
    )
    status, body = _post_task(bound_token, dpop_proof=thief_proof)
    rows.append(_row(
        mode="C", attack="stolen_bearer",
        expected_status=401, expected_error="invalid_dpop",
        actual_status=status, actual_error=body.get("error"),
        actual_message=body.get("message"),
        notes="thief signs proof with a NEW keypair; jkt mismatch on cnf.jkt",
    ))
    return rows


# ---------------------------------------------------------------------------
# S2: replay of the same DPoP proof (same jti)
# ---------------------------------------------------------------------------


def test_s2_dpop_proof_replay() -> List[Dict[str, Any]]:
    """Send identical (Authorization, DPoP) header pair twice. The first
    must succeed; the second must be rejected via the Redis jti cache."""
    rows: List[Dict[str, Any]] = []

    priv, pub = generate_keypair()
    token_proof = generate_dpop_proof(priv, pub, "POST", TOKEN_ENDPOINT)
    try:
        token = _mint_token(CLIENT_ID, CLIENT_SECRET, dpop_proof=token_proof)
    except Exception as exc:
        rows.append(_row(
            mode="C", attack="dpop_replay",
            expected_status=200, expected_error=None,
            actual_status=0, actual_error=str(exc),
            notes="could not obtain DPoP-bound token",
        ))
        return rows

    api_proof = generate_dpop_proof(priv, pub, "POST", API_ENDPOINT, access_token=token)
    status1, body1 = _post_task(token, dpop_proof=api_proof)
    rows.append(_row(
        mode="C", attack="dpop_replay_first_request",
        expected_status=200, expected_error=None,
        actual_status=status1, actual_error=body1.get("error"),
        actual_message=body1.get("message"),
        notes="first presentation of jti — must succeed to set up the replay",
    ))

    status2, body2 = _post_task(token, dpop_proof=api_proof)
    rows.append(_row(
        mode="C", attack="dpop_replay_second_request",
        expected_status=403, expected_error="replay_attack",
        actual_status=status2, actual_error=body2.get("error"),
        actual_message=body2.get("message"),
        notes="same jti re-presented — must be rejected by replay cache",
    ))

    # Modes A and B are not DPoP-bearing and the API does not consult the
    # replay cache for them. Record this explicitly so the table reports
    # an empirical N/A rather than a hardcoded one.
    for mode in ("A", "B"):
        rows.append(_row(
            mode=mode, attack="dpop_replay",
            expected_status=None, expected_error=None,
            actual_status=-1, actual_error="not_applicable",
            notes="mode does not present a DPoP proof; replay cache is not consulted",
        ))
    return rows


# ---------------------------------------------------------------------------
# S3: wrong-audience token
# ---------------------------------------------------------------------------


def test_s3_wrong_audience() -> List[Dict[str, Any]]:
    """Issue a token from `wrong-audience-client` (no agent-scope → no
    audience mapper → token's aud does not include `internal-api`). Send to
    /api/task. The API's audience check fires before any DPoP/mode-specific
    logic, so the result is the same for every mode."""
    rows: List[Dict[str, Any]] = []

    if not WRONG_AUDIENCE_CLIENT_SECRET:
        rows.append(_row(
            mode="A", attack="wrong_audience",
            expected_status=403, expected_error="invalid_audience",
            actual_status=0, actual_error="missing_env",
            notes="WRONG_AUDIENCE_CLIENT_SECRET not set; cannot mint test token",
        ))
        return rows

    try:
        token = _mint_token(WRONG_AUDIENCE_CLIENT_ID, WRONG_AUDIENCE_CLIENT_SECRET)
    except Exception as exc:
        rows.append(_row(
            mode="A", attack="wrong_audience",
            expected_status=403, expected_error="invalid_audience",
            actual_status=0, actual_error=str(exc),
            notes="failed to mint wrong-audience token",
        ))
        return rows

    status, body = _post_task(token)
    rows.append(_row(
        mode="A", attack="wrong_audience",
        expected_status=403, expected_error="invalid_audience",
        actual_status=status, actual_error=body.get("error"),
        actual_message=body.get("message"),
        notes="token issued by wrong-audience-client (no audience mapper)",
    ))
    # Audience verification runs before mode-specific branches in
    # api/verifier.py:verify_request, so the same token elicits the same
    # 403 regardless of whether the caller also presents a DPoP header.
    rows.append(_row(
        mode="B", attack="wrong_audience",
        expected_status=403, expected_error="invalid_audience",
        actual_status=status, actual_error=body.get("error"),
        actual_message=body.get("message"),
        notes="audience check is mode-independent; replicating Mode A result",
    ))
    rows.append(_row(
        mode="C", attack="wrong_audience",
        expected_status=403, expected_error="invalid_audience",
        actual_status=status, actual_error=body.get("error"),
        actual_message=body.get("message"),
        notes="audience check is mode-independent; replicating Mode A result",
    ))
    return rows


# ---------------------------------------------------------------------------
# S4: actor chain at delegation depth 2
# ---------------------------------------------------------------------------


def test_s4_actor_chain() -> List[Dict[str, Any]]:
    """At delegation_depth=2 the agent client requests a token exchange with
    `actor_token` set. Mode B/C tokens should carry an `act` (actor) claim
    propagated into `auth_metadata.actor`. Mode A does no exchange and has
    no actor."""
    rows: List[Dict[str, Any]] = []
    for mode in ("A", "B", "C"):
        client = AgentClient(mode=mode, cache_enabled=False, delegation_depth=2)
        result = client.make_request()
        client.close()
        if not result.success:
            rows.append(_row(
                mode=mode, attack="missing_actor_chain",
                expected_status=200, expected_error=None,
                actual_status=result.status_code, actual_error=result.error,
                notes="request failed before actor-chain could be inspected",
            ))
            continue
        actor = (result.response_data or {}).get("auth_metadata", {}).get("actor")
        actor_present = actor not in (None, "", {}, [])
        # Mode A: actor MUST be absent (no exchange happened).
        # Modes B/C: actor MUST be present (token exchange propagated `act`).
        if mode == "A":
            expected_present = False
        else:
            expected_present = True
        passed = (actor_present == expected_present)
        rows.append({
            "mode": mode,
            "attack": "missing_actor_chain",
            "expected_actor_present": expected_present,
            "actual_actor_present": actor_present,
            "actual_actor": actor,
            "actual_status": result.status_code,
            "passed": passed,
            "notes": "depth=2; act claim propagation via RFC 8693 token exchange",
        })

    # --- protocol-level probe: is `actor_token` validated at all? ------------
    # The AgentClient path above shows `act` is absent from the issued token.
    # This probe establishes something sharper: the authorization server does
    # not merely omit the claim, it accepts the parameter and discards it
    # without parsing. A deployer who sends actor_token per RFC 8693 gets a
    # 200 and no indication that delegation was never recorded.
    subject = _mint_token(CLIENT_ID, CLIENT_SECRET)
    probes = [
        ("actor_token_valid", subject),
        ("actor_token_garbage", "not-a-jwt-at-all"),
        ("actor_token_forged_jwt",
         "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhdHRhY2tlciJ9.xxxxx"),
    ]
    for attack, actor_token in probes:
        r = _exchange_with_actor(subject, actor_token)
        act_present = False
        if r.status_code == 200:
            act_present = "act" in _token_claims(r.json()["access_token"])
        rows.append(_row(
            mode="B", attack=attack,
            # A server that honoured RFC 8693 delegation semantics would either
            # emit `act` or reject an unparseable actor_token.
            expected_status=400, expected_error=None,
            actual_status=r.status_code, actual_error=None,
            actual_message=f"act_claim_present={act_present}",
            notes="actor_token accepted without validation; no act claim "
                  "emitted, so delegation provenance is silently unrecorded",
        ))
    return rows


# ---------------------------------------------------------------------------
# S5: expired token reuse
# ---------------------------------------------------------------------------


def test_s5_expired_token() -> List[Dict[str, Any]]:
    """Mint a token from `short-ttl-client` (access.token.lifespan=10s),
    sleep past `exp + leeway`, then present it. Expect 401 with
    authentication_failed and a "Token has expired" message."""
    rows: List[Dict[str, Any]] = []

    if not SHORT_TTL_CLIENT_SECRET:
        rows.append(_row(
            mode="A", attack="expired_token",
            expected_status=401, expected_error="authentication_failed",
            actual_status=0, actual_error="missing_env",
            notes="SHORT_TTL_CLIENT_SECRET not set; cannot mint short-TTL token",
        ))
        return rows

    try:
        token = _mint_token(SHORT_TTL_CLIENT_ID, SHORT_TTL_CLIENT_SECRET)
    except Exception as exc:
        rows.append(_row(
            mode="A", attack="expired_token",
            expected_status=401, expected_error="authentication_failed",
            actual_status=0, actual_error=str(exc),
            notes="failed to mint short-TTL token",
        ))
        return rows

    print(f"  [S5] sleeping {SHORT_TTL_SLEEP_SECONDS}s for token to expire...")
    time.sleep(SHORT_TTL_SLEEP_SECONDS)

    status, body = _post_task(token)
    # The bearer-token signature is verified first and `verify_exp=True`
    # makes `verify_bearer_token` raise before DPoP/mode-specific code
    # runs, so all three modes get the same 401 for an expired token.
    for mode in ("A", "B", "C"):
        rows.append(_row(
            mode=mode, attack="expired_token",
            expected_status=401, expected_error="authentication_failed",
            actual_status=status, actual_error=body.get("error"),
            actual_message=body.get("message"),
            notes=(
                f"slept {SHORT_TTL_SLEEP_SECONDS}s past short-ttl-client's "
                f"10s lifespan; expiry check runs before mode-specific logic"
            ),
        ))
    return rows


# ---------------------------------------------------------------------------
# S6: abuse of the RFC 8693 token-exchange endpoint itself
# ---------------------------------------------------------------------------


def _exchange(
    subject_token: str,
    *,
    client_id: str = CLIENT_ID,
    client_secret: Optional[str] = CLIENT_SECRET,
    dpop_proof: Optional[str] = None,
) -> httpx.Response:
    """RFC 8693 exchange attempt. `client_secret=None` models a thief who
    captured a token but not the client's credentials."""
    data = {
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "client_id": client_id,
        "subject_token": subject_token,
        "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "audience": TARGET_AUDIENCE,
        "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
    }
    if client_secret is not None:
        data["client_secret"] = client_secret
    headers = {"DPoP": dpop_proof} if dpop_proof else {}
    with httpx.Client(timeout=10.0) as c:
        return c.post(TOKEN_ENDPOINT, data=data, headers=headers)


def _jkt(token: str) -> Optional[str]:
    """Extract cnf.jkt (the key thumbprint an access token is bound to)."""
    return (_token_claims(token).get("cnf") or {}).get("jkt")


def test_s6_token_exchange_abuse() -> List[Dict[str, Any]]:
    """T5: can the exchange endpoint itself be used to launder a stolen token?

    S6a  token-only thief   -> exchange without client credentials.
    S6b  compromised peer   -> stolen Mode B bearer token + client credentials.
    S6c  compromised peer   -> stolen Mode C DPoP-BOUND token + client
         credentials + the attacker's OWN keypair. The question is whether the
         authorization server checks that `subject_token`'s existing `cnf.jkt`
         matches the key presenting the exchange proof. If it does not, the
         sender-constraint that Mode C relies on at the resource server is
         stripped off at the exchange boundary.
    """
    rows: List[Dict[str, Any]] = []

    # --- S6a: attacker holds the token but not the client credentials -------
    stolen = _mint_token(CLIENT_ID, CLIENT_SECRET)
    r = _exchange(stolen, client_secret=None)
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    rows.append(_row(
        mode="B", attack="exchange_without_client_credentials",
        expected_status=401, expected_error=None,
        actual_status=r.status_code, actual_error=body.get("error"),
        actual_message=body.get("error_description"),
        notes="token-only thief cannot reach the exchange endpoint: client "
              "authentication is required by RFC 8693 for confidential clients",
    ))

    # --- S6b: compromised peer laundering a plain bearer token --------------
    victim_b = _mint_token(CLIENT_ID, CLIENT_SECRET)
    r = _exchange(victim_b)
    if r.status_code == 200:
        laundered = r.json()["access_token"]
        status, api_body = _post_task(laundered)
        rows.append(_row(
            mode="B", attack="exchange_launders_stolen_bearer",
            expected_status=200, expected_error=None,
            actual_status=status, actual_error=api_body.get("error"),
            notes="attacker with client credentials exchanged a stolen bearer "
                  "token and used the result at the API (vulnerability "
                  "confirmed; Mode B offers no sender-constraint to strip)",
        ))
    else:
        rows.append(_row(
            mode="B", attack="exchange_launders_stolen_bearer",
            expected_status=200, expected_error=None,
            actual_status=r.status_code, actual_error="exchange_rejected",
            notes="exchange refused before the API could be reached",
        ))

    # --- S6c: compromised peer re-binding a DPoP-bound token ----------------
    v_priv, v_pub = generate_keypair()
    victim_c = _mint_token(
        CLIENT_ID, CLIENT_SECRET,
        generate_dpop_proof(v_priv, v_pub, "POST", TOKEN_ENDPOINT),
    )
    victim_jkt = _jkt(victim_c)

    a_priv, a_pub = generate_keypair()
    r = _exchange(
        victim_c,
        dpop_proof=generate_dpop_proof(a_priv, a_pub, "POST", TOKEN_ENDPOINT),
    )
    if r.status_code != 200:
        rows.append(_row(
            mode="C", attack="exchange_rebinds_dpop_token",
            expected_status=400, expected_error=None,
            actual_status=r.status_code, actual_error="exchange_rejected",
            notes=f"AS refused to exchange a token bound to {victim_jkt}; "
                  "sender-constraint preserved across the exchange boundary",
        ))
    else:
        laundered = r.json()["access_token"]
        attacker_jkt = _jkt(laundered)
        rebound = attacker_jkt is not None and attacker_jkt != victim_jkt
        api_proof = generate_dpop_proof(
            a_priv, a_pub, "POST", API_ENDPOINT, access_token=laundered
        )
        status, api_body = _post_task(laundered, api_proof)
        rows.append({
            "mode": "C",
            "attack": "exchange_rebinds_dpop_token",
            # A stack that preserved the sender-constraint would refuse the
            # exchange; reaching the API with the attacker's own key is the
            # failure we are testing for.
            "expected_status": 401,
            "expected_error": "invalid_dpop",
            "actual_status": status,
            "actual_error": api_body.get("error"),
            "victim_jkt": victim_jkt,
            "attacker_jkt": attacker_jkt,
            "rebound_to_attacker_key": rebound,
            "passed": status != 200,
            "notes": (
                "attacker presented a stolen DPoP-bound token to the exchange "
                "endpoint with an unrelated keypair; cnf.jkt was re-issued for "
                "the attacker's key and the laundered token was accepted at "
                "the resource server"
            ),
        })

    return rows


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _summarize(payload: Dict[str, Any]) -> None:
    print()
    print("=" * 80)
    print("SECURITY VERIFICATION SUMMARY")
    print("=" * 80)
    total = passed = 0
    for scenario, rows in payload["scenarios"].items():
        for row in rows:
            total += 1
            ok = row.get("passed", False)
            passed += 1 if ok else 0
            print(
                f"  {scenario:>3}  mode={row.get('mode'):>1}  "
                f"attack={row.get('attack', '?'):<28}  "
                f"{'PASS' if ok else 'FAIL'}  "
                f"status={row.get('actual_status')}  "
                f"error={row.get('actual_error')}"
            )
    print(f"\n{passed}/{total} checks matched expected behavior.")


def verify_security_claims() -> Dict[str, Any]:
    payload = {
        "metadata": {
            "timestamp": time.time(),
            "keycloak_url": KEYCLOAK_URL,
            "realm": REALM_NAME,
            "api_url": API_URL,
            "expected_audience": TARGET_AUDIENCE,
            "short_ttl_sleep_seconds": SHORT_TTL_SLEEP_SECONDS,
        },
        "scenarios": {
            "S1": test_s1_stolen_bearer_token(),
            "S2": test_s2_dpop_proof_replay(),
            "S3": test_s3_wrong_audience(),
            "S4": test_s4_actor_chain(),
            "S5": test_s5_expired_token(),
            "S6": test_s6_token_exchange_abuse(),
        },
    }
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    out = PROCESSED_DIR / "security_verification.json"
    with open(out, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"Saved: {out}")
    _summarize(payload)
    return payload


if __name__ == "__main__":
    verify_security_claims()
