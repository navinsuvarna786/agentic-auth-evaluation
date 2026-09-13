# Token-exchange behaviour: Keycloak 26.2.5 vs 26.6.4

Reproduce with `benchmark/probe_exchange_version_compare.py` against a
throwaway 26.6 instance (`docker run quay.io/keycloak/keycloak:26.6
start-dev --features=dpop`). 26.2.5 results come from the study testbed via
`benchmark/verify_security_claims.py` (scenario S6).

| Behaviour | 26.2.5 (testbed) | 26.6.4 (current) |
|---|---|---|
| DPoP-bound token as `subject_token` | accepted | **rejected**: `invalid_request` — "Sender-constrained tokens are not supported as subject_token. Use a Bearer token instead." |
| Exchange presenting a *different* key's proof | accepted, `cnf.jkt` re-issued for the presenting key (S6c) | rejected (same error) |
| Exchange presenting the *victim's own* proof | accepted, `cnf.jkt` preserved | rejected (same error) |
| Mode C constructible (exchanged token is sender-constrained) | yes, via DPoP-bound `subject_token` | yes, but only via a **bearer** `subject_token` + DPoP proof at the exchange; result carries `cnf.jkt` |
| Bearer `subject_token` laundering (S6b) | accepted | accepted (inherent to bearer subject tokens) |
| `actor_token` validated / `act` emitted | no — accepted and discarded, even when garbage | not retested; tracked upstream as keycloak#12076, keycloak#38279 (milestone 26.8.0) |

## Reading

S6c is **version-scoped**. It is demonstrable on 26.2.5 and remediated in
26.6, where the composition is refused outright rather than validated: 26.6
blocks the attack and the legitimate case with the same error. Nothing to
disclose upstream — the vendor has already changed the behaviour.

The conceptual point survives and is evidenced by the fix itself. The
interaction between RFC 8693 (exchange) and RFC 9449 (sender-constraining)
was under-specified enough that a production identity provider shipped a
re-binding path and then resolved it by forbidding the combination. Neither
specification assigns the authorization server responsibility for checking
that `subject_token`'s existing `cnf.jkt` matches the key presenting the
exchange proof.

Practitioner consequence: on >=26.6 the Mode C construction must start from a
bearer `subject_token`; carrying a sender-constrained token into the exchange
is refused.
