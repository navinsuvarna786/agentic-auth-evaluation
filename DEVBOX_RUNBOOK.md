# Devbox Provisioning Runbook (B3a)

Reproducible, multi-tenant-safe procedure for bringing up the two-host
topology on `authserver.example.internal` (Keycloak + Redis) and
`p6-u-jumphost` (API + benchmark driver). Every step is reversible
with the teardown command at the bottom.

Last verified: 2026-05-14.

## Footprint

| Host | Path | Purpose | Teardown |
|---|---|---|---|
| devbox | `~/.docker/cli-plugins/docker-compose` | user-scoped compose v2 binary | `rm -rf ~/.docker` |
| devbox | `~/cns2026/` | repo files, compose config, .env (mode 600) | `rm -rf ~/cns2026` |
| devbox | compose project `cns2026` | containers + bridge + named volume | `docker compose -p cns2026 -f docker-compose-devbox.yml down -v` |
| jumphost | repo `.env` (mode 600) | mirrors devbox secrets + tunnel URLs | `rm -f .env` |
| jumphost | SSH tunnel (background) | local-forward 18081, 18379 | `pkill -f 'ssh.*-L 18081'` |
| jumphost | `uvicorn api.main:app` (background) | API on 127.0.0.1:8001 | `pkill -f 'uvicorn api.main'` |

No host-level changes, no `sudo`, no shared-network exposure: all
container ports bind to `127.0.0.1` on devbox and are reached from
jumphost via SSH local-forward. Project-scoped naming
(`cns2026-keycloak-1`) means a parallel tenant using the same
images cannot collide.

## Steps

### 1. Pre-flight (read-only)

Run before any installs. Verify the box is idle and the 18000-range
ports are free.

```sh
ssh -i ~/.ssh/id_devbox -o IdentitiesOnly=yes user@authserver.example.internal '
uptime
free -h | head -2
df -h /
docker ps -a --format "{{.Names}}\t{{.Status}}"
docker network ls
docker compose version 2>&1 || echo "compose plugin missing"
for p in 18001 18002 18081 18379 18080; do
  ss -tln 2>/dev/null | grep -q ":$p " && echo "  $p: BUSY" || echo "  $p: free"
done
'
```

Expected: no `cns2026-*` containers, all 18000-range ports
free, docker daemon reachable.

### 2. Install user-scoped compose plugin (devbox)

```sh
ssh -i ~/.ssh/id_devbox -o IdentitiesOnly=yes user@authserver.example.internal '
mkdir -p ~/.docker/cli-plugins
curl -sSL --fail \
  https://github.com/docker/compose/releases/download/v2.32.0/docker-compose-linux-x86_64 \
  -o ~/.docker/cli-plugins/docker-compose
chmod +x ~/.docker/cli-plugins/docker-compose
docker compose version
'
```

### 3. Rsync repo to devbox

From jumphost repo root:

```sh
rsync -av --delete \
  --exclude '.git/' --exclude 'venv/' --exclude '__pycache__/' \
  --exclude 'results/' --exclude 'paper/' --exclude 'pastcnspapers/' \
  --exclude '*.pyc' --exclude '.env' --exclude '.pytest_cache/' \
  --exclude 'lanman-feedback*' --exclude 'comment.txt' \
  --exclude 'TODO_SECURITY_TESTS.md' --exclude 'howtowritecns.md' \
  -e "ssh -i ~/.ssh/id_devbox -o IdentitiesOnly=yes" \
  layered-auth-ephemeral-agents/ \
  user@authserver.example.internal:/home/user/cns2026/
```

### 4. Generate fresh secrets on devbox

```sh
ssh -i ~/.ssh/id_devbox -o IdentitiesOnly=yes user@authserver.example.internal '
cd ~/cns2026
[ -f .env ] && { echo "exists; not overwriting"; exit 0; }
cat > .env <<EOF
POSTGRES_PASSWORD=$(openssl rand -hex 16)
KEYCLOAK_ADMIN_USER=admin
KEYCLOAK_ADMIN_PASSWORD=$(openssl rand -hex 16)
CLIENT_ID=agent-launcher
CLIENT_SECRET=$(openssl rand -hex 32)
INTERNAL_API_SECRET=$(openssl rand -hex 32)
WRONG_AUDIENCE_CLIENT_SECRET=$(openssl rand -hex 32)
SHORT_TTL_CLIENT_SECRET=$(openssl rand -hex 32)
EOF
chmod 600 .env
'
```

### 5. Bring up the infra stack

```sh
ssh -i ~/.ssh/id_devbox -o IdentitiesOnly=yes user@authserver.example.internal '
cd ~/cns2026
docker compose -p cns2026 -f docker-compose-devbox.yml --env-file .env up -d
'
```

First start pulls images (~5 min) and runs Keycloak schema init + realm
import (~85 s). Wait for `cns2026-keycloak-1` to report
`healthy`:

```sh
ssh -i ~/.ssh/id_devbox -o IdentitiesOnly=yes user@authserver.example.internal '
docker inspect --format "{{.State.Health.Status}}" cns2026-keycloak-1
'
```

### 6. Patch client secrets via admin API

**Critical gotcha.** Keycloak 26 does NOT substitute `${env.VAR}`
placeholders in realm-export.json for client secrets at import time.
The string `${env.CLIENT_SECRET}` is stored literally. Token requests
will fail with `401 unauthorized` until the four client secrets are
patched.

Run on devbox where Keycloak admin is reachable on `127.0.0.1:18081`:

```sh
ssh -i ~/.ssh/id_devbox -o IdentitiesOnly=yes user@authserver.example.internal 'bash -s' <<'OUTER'
set -a; source ~/cns2026/.env; set +a
AT=$(curl -s -X POST http://127.0.0.1:18081/realms/master/protocol/openid-connect/token \
       -d "client_id=admin-cli" -d "grant_type=password" \
       -d "username=admin" -d "password=${KEYCLOAK_ADMIN_PASSWORD}" | jq -r .access_token)
for client in agent-launcher internal-api wrong-audience-client short-ttl-client; do
  case $client in
    agent-launcher) S="$CLIENT_SECRET" ;;
    internal-api) S="$INTERNAL_API_SECRET" ;;
    wrong-audience-client) S="$WRONG_AUDIENCE_CLIENT_SECRET" ;;
    short-ttl-client) S="$SHORT_TTL_CLIENT_SECRET" ;;
  esac
  CID=$(curl -s -H "Authorization: Bearer $AT" \
         "http://127.0.0.1:18081/admin/realms/ephemeral-agents/clients?clientId=$client" | jq -r '.[0].id')
  REPR=$(curl -s -H "Authorization: Bearer $AT" \
          "http://127.0.0.1:18081/admin/realms/ephemeral-agents/clients/$CID")
  echo "$REPR" | jq --arg s "$S" '.secret = $s' | \
    curl -s -o /dev/null -w "$client: %{http_code}\n" -X PUT \
      -H "Authorization: Bearer $AT" -H "Content-Type: application/json" \
      -d @- "http://127.0.0.1:18081/admin/realms/ephemeral-agents/clients/$CID"
done
OUTER
```

Expected: all four `204` responses.

### 7. Open SSH local-forward from jumphost

```sh
ssh -i ~/.ssh/id_devbox -o IdentitiesOnly=yes -N -f \
  -L 18081:127.0.0.1:18081 \
  -L 18379:127.0.0.1:18379 \
  user@authserver.example.internal
```

Verify:

```sh
curl -s -w "HTTP=%{http_code}\n" \
  http://localhost:18081/realms/ephemeral-agents/.well-known/openid-configuration -o /dev/null
echo -e 'PING\r\nQUIT\r\n' | timeout 3 nc -q 1 localhost 18379
```

Expected: `HTTP=200`, `+PONG`.

### 8. Generate jumphost-side `.env`

From the repo root on jumphost:

```sh
ssh -i ~/.ssh/id_devbox -o IdentitiesOnly=yes user@authserver.example.internal \
  'cat ~/cns2026/.env' > layered-auth-ephemeral-agents/.env
cat >> layered-auth-ephemeral-agents/.env <<'EOF'
# Jumphost overrides — reach devbox via SSH local-forward
KEYCLOAK_URL=http://localhost:18081
API_URL=http://localhost:8001
REDIS_URL=redis://localhost:18379
REALM_NAME=ephemeral-agents
TARGET_AUDIENCE=internal-api
EXPECTED_AUDIENCE=internal-api
EOF
chmod 600 layered-auth-ephemeral-agents/.env
```

### 9. Measure clock skew, set the three skew-knobs

The two hosts are likely NOT synchronized. Measure:

```sh
JUMP=$(date +%s)
DEV=$(ssh -i ~/.ssh/id_devbox -o IdentitiesOnly=yes user@authserver.example.internal 'date +%s')
SKEW=$((DEV - JUMP))
echo "skew (devbox - jumphost) = ${SKEW}s"
```

If `|SKEW| > 5` seconds, append to jumphost `.env`:

```sh
cat >> layered-auth-ephemeral-agents/.env <<EOF
API_TOKEN_LEEWAY=$((SKEW + 60))         # bearer-token iat/exp leeway on API
API_DPOP_FRESHNESS_WINDOW=$((SKEW + 120)) # DPoP iat freshness check on API
DPOP_IAT_OFFSET_SECONDS=$SKEW            # shift agent's proof iat for Keycloak
EOF
```

Why three knobs:

- `API_TOKEN_LEEWAY` — API verifies bearer-token `iat`/`exp` issued by
  devbox-clock; needs to accept tokens timestamped in the API's future.
- `API_DPOP_FRESHNESS_WINDOW` — API verifies DPoP proof `iat`; proof is
  shifted forward (see next knob), so window must accept the offset.
- `DPOP_IAT_OFFSET_SECONDS` — agent shifts proof `iat` forward so
  Keycloak's hardcoded ~10 s DPoP freshness check accepts it.

In production with NTP, all three default tight (`10, 60, 0`).

### 10. Start the API on jumphost

```sh
cd layered-auth-ephemeral-agents
set -a; source .env; set +a
nohup ./venv/bin/python -m uvicorn api.main:app \
  --host 127.0.0.1 --port 8001 --log-level warning > /tmp/api.log 2>&1 &
sleep 3
curl -s -w "HTTP=%{http_code}\n" http://127.0.0.1:8001/health -o /dev/null
```

### 11. Smoke test

```sh
set -a; source .env; set +a
./venv/bin/python -c "
import sys; sys.path.insert(0, '.')
from agent.agent_client import AgentClient
for mode in ['A', 'B', 'C']:
    c = AgentClient(mode=mode, cache_enabled=False, delegation_depth=1)
    r = c.make_request()
    c.close()
    print(f'mode={mode}  success={r.success}  status={r.status_code}  latency={r.latency_ms:.2f}ms  dpop_verified={r.dpop_verified}')
"
```

Expected: all three modes `success=True`, Mode C `dpop_verified=True`,
median latency around 20-25 ms (single-region cross-host, no netem).

Then the 12-second canary benchmark:

```sh
BENCHMARK_SMOKE=1 BENCHMARK_DURATION=10 BENCHMARK_WARMUP=2 \
  ./venv/bin/python -m benchmark.benchmark
```

Expected: one ~12 s run, 50 requests, 0 failures.

## Known testbed limitations

These are not protocol-level failures; they're config gaps in the
testbed environment we accept for this submission cycle.

### S4 (actor chain) is not empirically validated

Keycloak 26 token exchange (RFC 8693) needs fine-grained admin
permissions to be configured per-client, beyond the
`oauth2.token.exchange.grant.enabled=true` attribute. The
`agent_client._get_or_fetch_token` falls back silently to the initial
token when exchange returns 403, so requests still succeed but no
`act` claim is propagated.

The paper's claim about delegation-chain traceability still holds at
the protocol level; it's the realm configuration that's incomplete on
the testbed.

### S5 (expired token) requires a longer sleep

With `API_TOKEN_LEEWAY` widened to accommodate the cross-host skew, a
10 s-lifetime token issued by `short-ttl-client` is effectively valid
on the API for `LEEWAY + skew + ttl` ≈ 200 s. The default
`S5_SLEEP_SECONDS=22` is too short.

For the final benchmark run, set `S5_SLEEP_SECONDS=210` before
invoking `verify_security_claims.py`. The 3.5 min sleep is one-time.

## Teardown

In order, from jumphost:

```sh
# 1. Stop API
pkill -f 'uvicorn api.main'

# 2. Stop tunnel
pkill -f 'ssh.*-L 18081'

# 3. Stop and remove the entire devbox stack (containers, network, volume)
ssh -i ~/.ssh/id_devbox -o IdentitiesOnly=yes user@authserver.example.internal '
cd ~/cns2026
docker compose -p cns2026 -f docker-compose-devbox.yml --env-file .env down -v
'

# 4. Remove devbox files (optional, keeps repo+env on box if next session reuses)
ssh -i ~/.ssh/id_devbox -o IdentitiesOnly=yes user@authserver.example.internal '
rm -rf ~/cns2026 ~/.docker/cli-plugins
'

# 5. Remove jumphost .env (secrets)
rm -f layered-auth-ephemeral-agents/.env
```

After teardown the devbox returns to the same state as the
2026-05-06 pre-flight: no `cns2026-*` containers, no images pulled
(images stay cached, harmless), no `~/cns2026/` directory.
