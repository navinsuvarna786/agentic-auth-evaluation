# Performance and Security Tradeoffs in Layered Authentication for Ephemeral AI Agents

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

## Overview

This repository is the artifact for the paper below, accepted at the 18th IEEE International Conference on Advanced Infocomm Technology (ICAIT 2026), Auckland, 23–27 November 2026. It contains the prototype (Keycloak 26.2 authorization server, FastAPI resource server with DPoP verifier, Redis replay cache), the agent client for the three authentication modes, the benchmark and attack harnesses, every raw measurement behind the reported numbers, the analysis pipeline, and the LaTeX source of the paper.

**Paper Title:** Performance and Security Tradeoffs in Layered Authentication for Ephemeral Artificial Intelligence Agents

**Research Questions:**
- **RQ1:** What latency and throughput overhead is introduced by layered authentication compared with simpler bearer-token baselines?
- **RQ2:** How do token caching and agent churn affect performance and reliability?
- **RQ3:** What concrete security gains are achieved against replay, token theft, and audience misuse?

## Repository Structure

```
agentic-auth-evaluation/
├── docker-compose.yml          # Multi-container orchestration
├── Dockerfile                  # API service container
├── requirements.txt            # Python dependencies
├── README.md                   # This file
├── ARCHITECTURE.md             # Detailed architecture documentation
│
├── keycloak/                   # Keycloak configuration
│   ├── realm-export.json       # Realm configuration with token exchange
│   └── setup-notes.md          # Manual setup instructions
│
├── api/                        # Protected API service (FastAPI)
│   ├── main.py                 # FastAPI application
│   ├── verifier.py             # Token and DPoP verification
│   └── config.py               # Configuration
│
├── agent/                      # Agent client library
│   ├── agent_client.py         # Three authentication modes (A, B, C)
│   ├── dpop.py                 # DPoP proof generation (RFC 9449)
│   └── config.py               # Configuration
│
├── benchmark/                  # Benchmark harness
│   ├── launcher.py             # Ephemeral agent spawner
│   ├── benchmark.py            # Full experiment runner (108 runs)
│   ├── analyze_results.py      # Statistical analysis and plotting
│   └── attack_scenarios.py     # Security evaluation scenarios
│
├── results/                    # Experiment results (generated)
│   ├── raw/                    # CSV logs per run
│   ├── processed/              # Aggregated statistics
│   └── figures/                # Generated plots
│
└── paper/                      # IEEE conference paper
    ├── main.tex                # LaTeX source
    ├── references.bib          # Bibliography
    └── figures/                # Paper figures
```

## Quick Start

### Prerequisites

- **Docker 24.0+** with Compose V2
- **Python 3.11+**
- **Unix-like system** (Linux or macOS)
- **4GB RAM** minimum
- **10GB disk space**

### Installation

1. **Clone the repository:**
   ```bash
   git clone <repository-url>
   cd layered-auth-ephemeral-agents
   ```

2. **Install Python dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Start the infrastructure:**
   ```bash
   docker-compose up -d
   ```

4. **Wait for services to be ready (~30 seconds):**
   ```bash
   # Check service health
   docker-compose ps
   
   # Wait for Keycloak to be fully initialized
   until curl -f http://localhost:8080/health/ready; do
       echo "Waiting for Keycloak..."
       sleep 5
   done
   
   # Check API health
   curl http://localhost:8000/health
   ```

### Running a Quick Test

Test a single authentication mode:

```bash
# Test Mode A (Direct Bearer)
python -m benchmark.launcher
```

### Running the Full Benchmark

**Warning:** The full matrix takes approximately **3.8 hours** (195 runs × 70 seconds each: 33 cells × 5 replications plus the headline cell × 10 per mode). Set `BENCHMARK_SMOKE=1` for a single-cell canary first.

```bash
# Run the 195-run matrix (randomized order, seed 20260506)
python -m benchmark.benchmark
```

Progress will be displayed in real-time. Results are saved to `results/raw/`.

### Analyzing Results

After the benchmark completes:

```bash
# Generate statistics and plots
python -m benchmark.analyze_results
```

This will create:
- `results/processed/aggregated_statistics.csv` - Summary statistics per condition
- `results/processed/attack_mitigation_table.csv` - Security evaluation results
- `results/figures/latency_comparison.pdf` - Figure 1 for the paper
- `results/figures/throughput_churn.pdf` - Figure 2 for the paper

### Running Attack Scenarios

```bash
# Real HTTP attack tests S1-S6 against the running stack -> results/processed/security_verification.json
python -m benchmark.verify_security_claims

# Version-compatibility probe (S4, S6) against a throwaway Keycloak 26.6 instance -> results/processed/kc266_compat.json
python benchmark/probe_kc266_compat.py
```

## Authentication Modes

The prototype implements three authentication modes:

### Mode A: Direct Bearer Token
- Agent obtains access token via client credentials grant
- Token presented in `Authorization: Bearer {token}` header
- **Simplest baseline, minimal security**

### Mode B: Token Exchange Bearer
- Agent obtains initial token, then exchanges it for audience-specific token (RFC 8693)
- Improves audience narrowing and delegation context
- Token still bearer-style (not sender-constrained)

### Mode C: Token Exchange + Proof-of-Possession
- Same as Mode B, plus DPoP sender-constraint (RFC 9449)
- Agent generates ECDSA P-256 key pair
- Each request includes DPoP proof JWT with `htm`, `htu`, `ath`, `jti`
- API verifies proof and checks replay cache
- **Strongest security: blocks token theft and replay**

## Experiment Matrix

The full benchmark tests:

- **3 Authentication Modes:** A, B, C
- **2 Cache Modes:** Off (fresh token/key per request), On (reuse within TTL)
- **3 Agent Churn Rates:** 1, 5, 20 agents/second
- **2 Delegation Depths:** 1-hop, 2-hop
- **5 Replications** per condition, **10** for the headline cell (cache-off, churn 5, depth 1)

**Total:** 33 × 5 + 3 × 10 = **195 runs**

Each run: 10s warmup + 60s active measurement

## Key Results

All numbers are generated from artifacts under `results/`; see the paper for CIs and tests.

**RQ1: Latency overhead** (headline cell, two-host testbed, n = 3000 per mode)
- Mode C (exchange + DPoP) adds **3.85 ms** median over bearer-only (Cliff's δ = 0.80); Mode B adds 3.42 ms
- Token and key caching cuts Mode C's median 65 % to 3.44 ms (1.34 ms residual)
- Against a real LLM-backed request, authentication is **0.8 %** of end-to-end median latency (1.1 % at P95)
- Under injected RTT, Mode A scales ≈1×, Mode B ≈2×, Mode C ≈4× RTT (`results/processed/netem_sweep_reps.json`)

**RQ2: Load, cache and churn**
- 100 % success at every churn rate and at every stress cell to 512 RPS (≈417 000 requests)
- Mode C develops a **bimodal tail** between 256 and 384 RPS on a single-worker verifier; the tail is episodic, the verifier stays flat, and an effective service time S ≈ 1.95 ms places the onset at utilization ρ ≈ 0.5–0.75
- Four workers, or Poisson arrivals at matched ρ, each dissolve the episodes
- The authorization server saturates near 1 000 exchanges/s per host, CPU-bound (`results/processed/as_scale_*.json`)

**RQ3: Security** (`results/processed/security_verification.json`, `kc266_compat.json`)
- Mode C blocks stolen-token reuse (S1, 401) and same-`jti` replay (S2, 403); audience scoping rejects mis-routed tokens in every mode (S3, 403)
- **Gaps at the exchange endpoint:** the `act` claim is never recorded, even for a garbage `actor_token` (S4) — on Keycloak 26.2.5 *and* 26.6.4; a bearer subject token can be laundered given client credentials (S6b); a DPoP-bound subject token could be re-bound to the attacker's key on 26.2.5 (S6c), which 26.6.4 refuses outright
- Delegation depth 1–5 costs ≈4 ms per hop with no compounding (`results/processed/depth_sweep.json`)

## Compiling the Paper

```bash
cd paper
pdflatex main.tex
bibtex main
pdflatex main.tex
pdflatex main.tex
```

Output: `paper/main.pdf`

## Configuration

### Environment Variables

**API Service:**
- `KEYCLOAK_URL` - Keycloak base URL (default: `http://localhost:8080`)
- `REDIS_URL` - Redis connection URL (default: `redis://localhost:6379`)
- `REALM_NAME` - Keycloak realm (default: `ephemeral-agents`)
- `EXPECTED_AUDIENCE` - Expected audience claim (default: `internal-api`)

**Agent Client:**
- `CLIENT_ID` - OAuth client ID (default: `agent-launcher`)
- `CLIENT_SECRET` - OAuth client secret. **Required**, must be set in `.env` (no default; see `.env.example`).
- `INTERNAL_API_SECRET` - API client secret. **Required**, must be set in `.env`.
- `KEYCLOAK_ADMIN_PASSWORD` - Keycloak admin bootstrap password. **Required**, must be set in `.env`.
- `POSTGRES_PASSWORD` - Postgres password backing Keycloak. **Required**, must be set in `.env`.
- `API_URL` - Protected API base URL (default: `http://localhost:8000`)

### First-time setup

```bash
cp .env.example .env       # then edit .env with non-default values
docker compose up -d
```

The compose files use `${VAR:?msg}` syntax — startup will fail loudly if any required variable is unset.

### Keycloak Configuration

The realm is automatically imported from `keycloak/realm-export.json`. If import fails, see `keycloak/setup-notes.md` for manual setup instructions.

**Key settings:**
- Token lifetime: 300 seconds (5 minutes)
- Token exchange: Enabled via `oauth2.token.exchange.grant.enabled=true`
- DPoP binding: Enabled via `dpop.bound.access.tokens=true`

## Troubleshooting

### Keycloak not starting
```bash
# Check logs
docker logs auth-keycloak

# Restart services
docker-compose down
docker-compose up -d
```

### Token exchange failing
- Verify Keycloak 26.2+ is used (standard token exchange support)
- Check client configuration in Keycloak admin console
- Ensure `oauth2.token.exchange.grant.enabled` is set

### DPoP verification failing
- Check DPoP proof JWT format and signature
- Verify `cnf.jkt` claim in access token matches public key thumbprint
- Ensure Redis is running for replay cache

### Redis connection issues
- Verify Redis container is healthy: `docker-compose ps`
- Check Redis logs: `docker logs auth-redis`
- API degrades gracefully if Redis is unavailable (replay check skipped)

## Performance Notes

**Deployment:** The paper's headline and stress numbers use loopback co-location; the RTT sweep and the study testbed use a two-host topology (`docker-compose-devbox.yml`, `DEVBOX_RUNBOOK.md`). Production deployments should use:
- Horizontal scaling for Keycloak (multiple instances behind load balancer)
- Redis clustering for high-availability replay cache
- Separate hosts for API service and infrastructure

**Expected resource usage:**
- CPU: ~2 cores during benchmark
- Memory: ~2GB total across all containers
- Disk: ~500MB for logs and results

## Security Notes

**This is a research prototype, not production-ready code.**

For production use:
- Enable TLS for all communication
- Use proper secrets management (not environment variables)
- Implement rate limiting and DDoS protection
- Add comprehensive logging and monitoring
- Perform security audit and penetration testing
- Implement key rotation policies
- Use production-grade Redis with persistence and clustering

## Citation

```bibtex
@inproceedings{suvarna2026layeredauth,
  title     = {Performance and Security Tradeoffs in Layered Authentication for Ephemeral Artificial Intelligence Agents},
  author    = {Suvarna, Navin and Pappu, Karthik and Sharma, Saurabh and Parashar, Ankit},
  booktitle = {Proceedings of the 18th IEEE International Conference on Advanced Infocomm Technology (ICAIT)},
  address   = {Auckland, New Zealand},
  month     = nov,
  year      = {2026},
  publisher = {IEEE}
}
```

## License

Apache License 2.0 — see [LICENSE](LICENSE).

## Contact

Open a GitHub issue, or contact the corresponding author: Navin Suvarna (nsuvarna@cisco.com).

## Acknowledgments

This work builds on:
- OAuth 2.0 (RFC 6749)
- OpenID Connect
- RFC 8693 (Token Exchange)
- RFC 9449 (DPoP)
- SPIFFE workload identity
- NIST Zero Trust Architecture (SP 800-207)
- Keycloak open source identity and access management

## References

See `paper/references.bib` for complete bibliography.
