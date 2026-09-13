"""
API Configuration Module
"""
import os

# Keycloak settings
KEYCLOAK_URL = os.getenv("KEYCLOAK_URL", "http://localhost:8081")
REALM_NAME = os.getenv("REALM_NAME", "ephemeral-agents")
EXPECTED_AUDIENCE = os.getenv("EXPECTED_AUDIENCE", "internal-api")

# Redis settings
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

# API settings
API_PORT = int(os.getenv("API_PORT", "8000"))
API_HOST = os.getenv("API_HOST", "0.0.0.0")

# Token validation settings.
# TOKEN_LEEWAY must accommodate any clock skew between the AS host (Keycloak)
# and the RS host (this API). On research testbeds without NTP synchronization
# the skew can reach a minute or more; in production deployments NTP keeps
# it under one second and the default 10 s is plenty. Override via env var
# `API_TOKEN_LEEWAY` when running cross-host without synced clocks.
TOKEN_LEEWAY = int(os.getenv("API_TOKEN_LEEWAY", "10"))
REPLAY_CACHE_TTL = int(os.getenv("API_REPLAY_CACHE_TTL", "360"))  # seconds (token lifetime + buffer)

# Extra freshness window applied to a DPoP proof's `iat` claim, on top of
# PyJWT's exp check. The default 60 is the standard DPoP replay-resistance
# budget; testbeds with unsynchronized clocks (we measured 69 s between
# jumphost and devbox) need this widened. Override via env var.
DPOP_FRESHNESS_WINDOW_SECONDS = int(os.getenv("API_DPOP_FRESHNESS_WINDOW", "60"))

# Keycloak public keys endpoint
JWKS_URL = f"{KEYCLOAK_URL}/realms/{REALM_NAME}/protocol/openid-connect/certs"
ISSUER = f"{KEYCLOAK_URL}/realms/{REALM_NAME}"
