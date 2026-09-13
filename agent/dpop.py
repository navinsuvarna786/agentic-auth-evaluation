"""
DPoP (Demonstrating Proof-of-Possession) Implementation per RFC 9449
"""
import os
import time
import uuid
import hashlib
import base64
import json
from typing import Dict, Any, Tuple, Optional
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend
import jwt


def generate_keypair() -> Tuple[Any, Any]:
    """
    Generate an ECDSA P-256 key pair for DPoP.

    Returns:
        Tuple of (private_key, public_key)
    """
    private_key = ec.generate_private_key(ec.SECP256R1(), default_backend())
    public_key = private_key.public_key()
    return private_key, public_key


def public_key_to_jwk(public_key) -> Dict[str, str]:
    """
    Convert ECDSA public key to JWK format.

    Args:
        public_key: ECDSA public key object

    Returns:
        JWK dictionary
    """
    # Get public numbers
    public_numbers = public_key.public_numbers()

    # Convert coordinates to base64url
    def int_to_base64url(n: int, length: int) -> str:
        byte_length = (length + 7) // 8
        n_bytes = n.to_bytes(byte_length, byteorder='big')
        return base64.urlsafe_b64encode(n_bytes).decode('ascii').rstrip('=')

    x = int_to_base64url(public_numbers.x, 256)
    y = int_to_base64url(public_numbers.y, 256)

    return {
        "kty": "EC",
        "crv": "P-256",
        "x": x,
        "y": y
    }


def compute_jwk_thumbprint(jwk: Dict[str, str]) -> str:
    """
    Compute JWK thumbprint per RFC 7638.

    Args:
        jwk: JSON Web Key dictionary

    Returns:
        Base64url-encoded SHA-256 thumbprint
    """
    # Create canonical JSON with required members only
    required = {
        "crv": jwk["crv"],
        "kty": jwk["kty"],
        "x": jwk["x"],
        "y": jwk["y"]
    }

    canonical = json.dumps(required, sort_keys=True, separators=(',', ':'))
    hash_bytes = hashlib.sha256(canonical.encode('utf-8')).digest()
    return base64.urlsafe_b64encode(hash_bytes).decode('ascii').rstrip('=')


def compute_access_token_hash(access_token: str) -> str:
    """
    Compute SHA-256 hash of access token for ath claim.

    Args:
        access_token: The access token string

    Returns:
        Base64url-encoded SHA-256 hash
    """
    hash_bytes = hashlib.sha256(access_token.encode('ascii')).digest()
    return base64.urlsafe_b64encode(hash_bytes).decode('ascii').rstrip('=')


# DPoP proof validity window. RFC 9449 §4.3 recommends a short, server-verified
# expiration; the verifier still applies its own freshness check (defence in
# depth), so this window only sets the upper bound.
DPOP_PROOF_LIFETIME_SECONDS = 60

# Optional offset applied to the proof's iat claim. Used only to work around
# clock skew between the agent host and the authorization server: on
# unsynchronized testbeds Keycloak's ~10 s DPoP freshness check rejects
# proofs whose iat is too far in its past ("DPoP proof is not active"), so
# we shift iat forward by the measured skew. In production deployments the
# default is 0 and clock sync (NTP/PTP) is assumed.
DPOP_IAT_OFFSET_SECONDS = int(os.getenv("DPOP_IAT_OFFSET_SECONDS", "0"))


def generate_dpop_proof(
    private_key,
    public_key,
    method: str,
    url: str,
    access_token: Optional[str] = None,
    proof_lifetime_seconds: int = DPOP_PROOF_LIFETIME_SECONDS,
) -> str:
    """
    Generate a DPoP proof JWT per RFC 9449.

    Args:
        private_key: ECDSA private key
        public_key: ECDSA public key
        method: HTTP method (e.g., "POST")
        url: Full URL of the request
        access_token: Access token (for ath claim), optional
        proof_lifetime_seconds: exp - iat window. Default 60s.

    Returns:
        DPoP proof JWT string
    """
    # Convert public key to JWK
    jwk = public_key_to_jwk(public_key)

    # Create JWT header
    header = {
        "typ": "dpop+jwt",
        "alg": "ES256",
        "jwk": jwk
    }

    # Create JWT payload. Per RFC 9449 §4.2, jti, htm, htu, iat are required;
    # ath is required when binding to an access token. exp is RECOMMENDED and
    # bounds the replay window even if the server's freshness check is misconfigured.
    # DPOP_IAT_OFFSET_SECONDS lets us compensate for measured clock skew
    # between hosts on unsynchronized testbeds (default 0).
    iat = int(time.time()) + DPOP_IAT_OFFSET_SECONDS
    payload = {
        "jti": str(uuid.uuid4()),
        "htm": method,
        "htu": url,
        "iat": iat,
        "exp": iat + proof_lifetime_seconds,
    }

    # Add ath claim if access token is provided
    if access_token:
        payload["ath"] = compute_access_token_hash(access_token)

    # Sign JWT with private key
    dpop_jwt = jwt.encode(
        payload,
        private_key,
        algorithm="ES256",
        headers=header
    )

    return dpop_jwt


def get_jkt_from_keypair(public_key) -> str:
    """
    Get JWK thumbprint (jkt) from a public key.

    Args:
        public_key: ECDSA public key

    Returns:
        JWK thumbprint string
    """
    jwk = public_key_to_jwk(public_key)
    return compute_jwk_thumbprint(jwk)


# Type hints
from typing import Optional
