"""
Agent Configuration Module
"""
import os


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"Required environment variable {name!r} is not set. "
            f"Copy .env.example to .env and fill in values, or export the variable."
        )
    return value


# Keycloak settings (URLs are non-secret, defaults are dev-friendly)
KEYCLOAK_URL = os.getenv("KEYCLOAK_URL", "http://localhost:8081")
REALM_NAME = os.getenv("REALM_NAME", "ephemeral-agents")

# Client credentials — SECRET must come from environment.
CLIENT_ID = os.getenv("CLIENT_ID", "agent-launcher")
CLIENT_SECRET = _require("CLIENT_SECRET")

# API settings
API_URL = os.getenv("API_URL", "http://localhost:8001")
API_ENDPOINT = f"{API_URL}/api/task"

# Token settings
TARGET_AUDIENCE = os.getenv("TARGET_AUDIENCE", "internal-api")

# Token endpoint
TOKEN_ENDPOINT = f"{KEYCLOAK_URL}/realms/{REALM_NAME}/protocol/openid-connect/token"
