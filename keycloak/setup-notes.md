# Keycloak Setup Notes

## Realm Configuration

The `realm-export.json` file configures the `ephemeral-agents` realm with:

### Clients

1. **agent-launcher** (Confidential Client)
   - Client ID: `agent-launcher`
   - Client Secret: `$CLIENT_SECRET`
   - Grant Types: Client Credentials, Token Exchange
   - Token Exchange: Enabled
   - DPoP Bound Tokens: Enabled
   - Purpose: Obtains tokens for ephemeral agents

2. **internal-api** (Bearer-Only Client)
   - Client ID: `internal-api`
   - Client Secret: `$INTERNAL_API_SECRET` (substituted from environment at realm import time)
   - Bearer Only: Yes
   - Purpose: Resource server, validates tokens

### Token Settings

- Access Token Lifespan: 300 seconds (5 minutes)
- No refresh tokens (simplified for ephemeral agents)
- DPoP support enabled for sender-constrained tokens

### Token Exchange Configuration

Token exchange is enabled via:
```
oauth2.token.exchange.grant.enabled = true
```

This allows RFC 8693 token exchange:
```
POST /realms/ephemeral-agents/protocol/openid-connect/token
Content-Type: application/x-www-form-urlencoded

grant_type=urn:ietf:params:oauth:grant-type:token-exchange
&client_id=agent-launcher
&client_secret=$CLIENT_SECRET
&subject_token={initial_token}
&subject_token_type=urn:ietf:params:oauth:token-type:access_token
&audience=internal-api
&requested_token_type=urn:ietf:params:oauth:token-type:access_token
```

### DPoP Configuration

DPoP (Demonstrating Proof-of-Possession) is enabled via:
```
dpop.bound.access.tokens = true
```

When requesting a token with a DPoP proof header, Keycloak will:
1. Validate the DPoP proof JWT
2. Bind the access token to the public key via `cnf.jkt` claim
3. Return a sender-constrained access token

DPoP Proof Header Format:
```
DPoP: {base64url-encoded JWT}

JWT Header:
{
  "typ": "dpop+jwt",
  "alg": "ES256",
  "jwk": {public key}
}

JWT Payload:
{
  "jti": "{unique-id}",
  "htm": "POST",
  "htu": "{token-endpoint-url}",
  "iat": {timestamp}
}
```

### Manual Setup (if realm import fails)

If the realm export doesn't import automatically:

1. Access Keycloak admin console: http://localhost:8080
2. Login: admin / `$KEYCLOAK_ADMIN_PASSWORD` (set in `.env`)
3. Create realm: `ephemeral-agents`
4. Create clients as described above
5. Enable token exchange in client settings
6. Enable DPoP in client settings (Advanced Settings → OAuth 2.0 DPoP Bound Access Tokens)

### Testing Token Exchange

First source `.env` so `$CLIENT_SECRET` resolves: `set -a && . ./.env && set +a`

```bash
# Get initial token
curl -X POST http://localhost:8080/realms/ephemeral-agents/protocol/openid-connect/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=client_credentials" \
  -d "client_id=agent-launcher" \
  -d "client_secret=$CLIENT_SECRET"

# Exchange token
curl -X POST http://localhost:8080/realms/ephemeral-agents/protocol/openid-connect/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=urn:ietf:params:oauth:grant-type:token-exchange" \
  -d "client_id=agent-launcher" \
  -d "client_secret=$CLIENT_SECRET" \
  -d "subject_token={TOKEN_FROM_STEP_1}" \
  -d "subject_token_type=urn:ietf:params:oauth:token-type:access_token" \
  -d "audience=internal-api" \
  -d "requested_token_type=urn:ietf:params:oauth:token-type:access_token"
```

### Troubleshooting

**Token exchange not working:**
- Verify `oauth2.token.exchange.grant.enabled` is set to `true`
- Check Keycloak logs: `docker logs auth-keycloak`
- Ensure Keycloak 26.2+ is used (standard token exchange support)

**DPoP validation failing:**
- Verify `dpop.bound.access.tokens` is enabled
- Check DPoP proof JWT format and signature
- Ensure `jkt` claim in access token matches public key thumbprint

**Realm import failing:**
- Check JSON syntax in realm-export.json
- Manually create realm and clients
- Verify Keycloak version compatibility
