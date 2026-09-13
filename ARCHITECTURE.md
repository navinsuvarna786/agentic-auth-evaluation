# Repository Architecture Blueprint

## Directory Structure

```
layered-auth-ephemeral-agents/
├── docker-compose.yml
├── requirements.txt
├── README.md
├── ARCHITECTURE.md
├── keycloak/
│   ├── realm-export.json
│   └── setup-notes.md
├── api/
│   ├── __init__.py
│   ├── main.py              # FastAPI protected service
│   ├── verifier.py          # DPoP and audience verification
│   └── config.py            # API configuration
├── agent/
│   ├── __init__.py
│   ├── agent_client.py      # Three authentication modes
│   ├── dpop.py              # Proof-of-possession JWT generation
│   └── config.py            # Agent configuration
├── benchmark/
│   ├── __init__.py
│   ├── launcher.py          # Ephemeral agent spawner
│   ├── benchmark.py         # Experiment harness
│   ├── analyze_results.py   # Statistical analysis and plotting
│   └── attack_scenarios.py  # Security evaluation scenarios
├── results/
│   ├── raw/                 # CSV logs per run
│   ├── processed/           # Aggregated statistics
│   └── figures/             # Generated plots
├── paper/
│   ├── main.tex             # IEEE conference paper
│   ├── references.bib       # Bibliography
│   ├── figures/
│   │   └── architecture.pdf # Architecture diagram
│   └── IEEEtran.cls         # IEEE style file
└── scripts/
    ├── setup.sh             # Initial setup script
    └── run_full_benchmark.sh # Complete benchmark execution
```

## Component Architecture

### 1. Infrastructure Layer (Docker Compose)

**Services:**
- `keycloak`: OAuth 2.0/OIDC authorization server (Keycloak 26.2)
- `postgres`: Keycloak database backend
- `redis`: Replay nonce cache
- `api`: Protected internal service (FastAPI)

**Network:**
- Single bridge network `auth-net` for all services
- Internal DNS resolution for service discovery

### 2. Authorization Server (Keycloak)

**Configuration:**
- Realm: `ephemeral-agents`
- Client: `agent-launcher` (confidential, token exchange enabled)
- Client: `internal-api` (resource server, audience validation)
- Token exchange policy: allow `agent-launcher` → `internal-api`
- Token settings:
  - Access token lifespan: 300s
  - Refresh token: disabled for simplicity
  - DPoP support: enabled via custom configuration

**Endpoints:**
- Token endpoint: `/realms/ephemeral-agents/protocol/openid-connect/token`
- Token exchange: same endpoint with `grant_type=urn:ietf:params:oauth:grant-type:token-exchange`

### 3. Protected API Service

**FastAPI Application (api/main.py):**
- Endpoint: `POST /api/task`
- Authentication: Bearer token + optional DPoP proof
- Verification steps:
  1. Extract Authorization header
  2. Validate JWT signature and claims (iss, aud, exp, iat)
  3. If DPoP header present, verify proof JWT:
     - htm, htu, ath (access token hash)
     - jti (check replay cache)
     - Signature with cnf.jkt from access token
  4. Return success or specific error code

**Verifier Module (api/verifier.py):**
- `verify_bearer_token()`: Basic JWT validation
- `verify_dpop_proof()`: Full DPoP verification per RFC 9449
- `check_replay_cache()`: Redis-based jti tracking
- `verify_audience()`: Ensure aud matches expected value

### 4. Agent Client Library

**Agent Client (agent/agent_client.py):**

**Mode A: Direct Bearer**
```python
def mode_a_request(config):
    # 1. Get access token from Keycloak
    token = get_access_token(config)
    # 2. Call API with bearer token
    response = call_api(token)
    return response
```

**Mode B: Token Exchange Bearer**
```python
def mode_b_request(config):
    # 1. Get initial access token
    initial_token = get_access_token(config)
    # 2. Exchange for audience-specific token
    exchanged_token = exchange_token(initial_token, audience="internal-api")
    # 3. Call API with exchanged bearer token
    response = call_api(exchanged_token)
    return response
```

**Mode C: Token Exchange + DPoP**
```python
def mode_c_request(config, keypair):
    # 1. Get initial access token
    initial_token = get_access_token(config)
    # 2. Exchange for sender-constrained token (with cnf claim)
    exchanged_token = exchange_token(initial_token, audience="internal-api", dpop_proof=True)
    # 3. Generate DPoP proof JWT
    dpop_proof = generate_dpop_proof(keypair, method="POST", url="...", access_token=exchanged_token)
    # 4. Call API with DPoP header
    response = call_api(exchanged_token, dpop_proof=dpop_proof)
    return response
```

**DPoP Module (agent/dpop.py):**
- `generate_keypair()`: ECDSA P-256 key generation
- `generate_dpop_proof()`: Create proof JWT with htm, htu, ath, jti, iat
- `compute_jkt()`: JWK thumbprint per RFC 7638

### 5. Benchmark Harness

**Launcher (benchmark/launcher.py):**
- Spawns ephemeral agents as asyncio tasks or subprocesses
- Configurable churn rate (1, 5, 20 agents/second)
- Each agent:
  - Gets unique identity/credentials (simulated)
  - Makes 1-5 API requests
  - Terminates after completion

**Benchmark Runner (benchmark/benchmark.py):**
- Experiment matrix: 3 modes × 2 cache × 3 churn × 2 delegation = 36 conditions
- 3 replications per condition = 108 runs
- Each run: 60s active + 10s warmup
- Logging: CSV with columns:
  - timestamp, run_id, mode, cache, churn, delegation_depth
  - request_id, status, latency_ms, exchange_ms, verification_ms
  - replay_flag, audience_ok, actor_chain_present

**Analysis Script (benchmark/analyze_results.py):**
- Load all CSV files from results/raw/
- Compute per-condition statistics:
  - Median latency, P95 latency
  - Throughput (requests/second)
  - Failure rate (%)
- Generate figures:
  - Figure 1: Latency comparison (mode × cache)
  - Figure 2: Throughput vs churn level
- Export attack scenario results for table

**Attack Scenarios (benchmark/attack_scenarios.py):**
- S1: Stolen bearer token replay (different client)
- S2: DPoP proof replay (same jti)
- S3: Wrong audience token
- S4: Missing actor chain (delegation depth 2)
- S5: Expired token reuse

### 6. Paper Generation

**LaTeX Structure (paper/main.tex):**
- IEEE conference format (`\documentclass[conference]{IEEEtran}`)
- Two-column layout, 10pt font
- Sections as specified in requirements
- Figures: TikZ or included PDFs
- Tables: LaTeX tabular environment
- Bibliography: BibTeX with IEEEtran style

**Figures:**
- `architecture.pdf`: System architecture diagram
- `latency_comparison.pdf`: Generated by analyze_results.py
- `throughput_churn.pdf`: Generated by analyze_results.py

## Data Flow

### Mode A: Direct Bearer
```
Agent → Keycloak (token request)
      → API (bearer token in Authorization header)
      → Response
```

### Mode B: Token Exchange Bearer
```
Agent → Keycloak (initial token request)
      → Keycloak (token exchange with audience)
      → API (exchanged bearer token)
      → Response
```

### Mode C: Token Exchange + DPoP
```
Agent → Generate ECDSA keypair
      → Keycloak (initial token request)
      → Keycloak (token exchange with DPoP proof, get cnf-bound token)
      → Generate DPoP proof JWT (htm, htu, ath, jti)
      → API (token + DPoP header)
      → API verifies proof, checks replay cache
      → Response
```

## Security Properties by Mode

| Property | Mode A | Mode B | Mode C |
|----------|--------|--------|--------|
| Audience narrowing | ❌ | ✅ | ✅ |
| Delegation context | ❌ | ✅ | ✅ |
| Sender constraint | ❌ | ❌ | ✅ |
| Replay resistance | ❌ | ❌ | ✅ |
| Token theft mitigation | ❌ | ❌ | ✅ |

## Performance Hypotheses

**H1:** Mode C introduces 10-50ms additional latency vs Mode A due to:
- Token exchange round-trip (~5-15ms)
- DPoP proof generation (~1-5ms)
- DPoP verification + replay check (~5-10ms)

**H2:** Caching reduces overhead by 50-80% for Mode C by:
- Reusing exchanged tokens within TTL
- Reusing keypairs across requests

**H3:** Higher churn increases failure rate due to:
- Keycloak connection pool saturation
- Redis replay cache contention
- Increased token exchange load

## Implementation Notes

### Token Caching Strategy
- **Cache Off**: Fresh token + keypair per request
- **Cache On**: 
  - Token reuse within 60s window
  - Keypair reuse for agent lifetime (5-10 requests)
  - New jti per request even with cached token

### Delegation Depth Simulation
- **D1 (one-hop)**: subject = agent, no actor claim
- **D2 (two-hop)**: subject = agent, actor = launcher (simulated via token exchange `actor_token` parameter)

### Replay Cache Implementation
- Redis SET with TTL = token lifetime + 60s
- Key: `replay:jti:{jti_value}`
- Value: timestamp
- Check: EXISTS before processing, SET after success

### Measurement Precision
- Use `time.perf_counter()` for sub-millisecond precision
- Measure separately:
  - Total request latency (end-to-end)
  - Token exchange time (if applicable)
  - API verification time (server-side logging)

## Reproducibility Requirements

### Prerequisites
- Docker 24.0+ with Compose V2
- Python 3.11+
- Unix-like system (Linux/macOS)
- 4GB RAM minimum
- 10GB disk space

### Setup Steps
1. `./scripts/setup.sh` - Install dependencies, build containers
2. `docker-compose up -d` - Start services
3. Wait 30s for Keycloak initialization
4. `python benchmark/benchmark.py` - Run experiments
5. `python benchmark/analyze_results.py` - Generate plots
6. `cd paper && pdflatex main.tex && bibtex main && pdflatex main.tex && pdflatex main.tex`

### Expected Runtime
- Full benchmark (108 runs × 70s): ~2 hours
- Quick test (1 replication): ~40 minutes

## Limitations

1. **Single-host deployment**: No distributed system realism
2. **Simplified threat model**: No model-specific attacks
3. **Limited delegation depth**: Max 2 hops
4. **No cross-domain federation**: Single trust domain only
5. **Synthetic workload**: Not production-representative
6. **No formal verification**: Empirical evaluation only

## Next Steps (Phase 3)

1. Generate all code files
2. Write complete LaTeX paper with placeholders
3. Create architecture diagram
4. Write comprehensive README
5. Test docker-compose setup
6. Validate paper compiles
7. Package deliverables
