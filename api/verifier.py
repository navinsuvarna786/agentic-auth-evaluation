"""
Token and DPoP Verification Module
"""
import jwt
import hashlib
import base64
import json
import time
from typing import Optional, Dict, Any, Tuple
from urllib.parse import urlsplit, urlunsplit
from jwt import PyJWKClient
import redis
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.backends import default_backend


def normalize_htu(url: str) -> str:
    """Normalize a URL for DPoP htu comparison per RFC 9449 §4.3.

    - Lowercase scheme and host
    - Drop default ports (80 for http, 443 for https)
    - Strip query and fragment (RFC 9449: server MUST NOT compare them)
    - Collapse empty path to "/"
    """
    s = urlsplit(url)
    scheme = (s.scheme or "").lower()
    host = (s.hostname or "").lower()
    port = s.port
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        netloc = host
    elif port is not None:
        netloc = f"{host}:{port}"
    else:
        netloc = host
    path = s.path or "/"
    return urlunsplit((scheme, netloc, path, "", ""))

from api.config import (
    JWKS_URL,
    ISSUER,
    EXPECTED_AUDIENCE,
    TOKEN_LEEWAY,
    REDIS_URL,
    REPLAY_CACHE_TTL,
    DPOP_FRESHNESS_WINDOW_SECONDS,
)


class VerificationError(Exception):
    """Base class for verification errors"""
    pass


class ReplayAttackError(VerificationError):
    """Raised when a replay attack is detected"""
    pass


class ReplayCacheUnavailableError(VerificationError):
    """Raised when the replay cache (Redis) cannot be consulted.

    The API maps this to HTTP 503 (not 403) so operators get a clear signal
    that the security check itself is degraded, rather than a misleading
    "replay detected" response. Mode C requests are rejected — fail-closed.
    """
    pass


class AudienceError(VerificationError):
    """Raised when audience validation fails"""
    pass


class DPoPError(VerificationError):
    """Raised when DPoP validation fails"""
    pass


# Initialize Redis client for replay cache
try:
    redis_client = redis.from_url(REDIS_URL, decode_responses=True)
except Exception as e:
    print(f"Warning: Redis connection failed: {e}")
    redis_client = None


# Initialize JWKS client for Keycloak public keys
jwks_client = PyJWKClient(JWKS_URL)


def verify_bearer_token(token: str, timings: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """
    Verify a bearer access token from Keycloak.

    If `timings` is provided, populate it with stage-level latency for
    diagnostic decomposition: jwks_ms (JWKS-key lookup, cached after
    first hit by PyJWKClient), jwt_decode_ms (RS256/ES256 signature
    verify + claim parse). Used to investigate per-stage queueing
    during the bimodal stress regime; the public-facing API contract
    is unchanged.
    """
    try:
        # Get signing key from JWKS
        t0 = time.perf_counter()
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        if timings is not None:
            timings["jwks_ms"] = (time.perf_counter() - t0) * 1000

        # Decode and verify token
        t1 = time.perf_counter()
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256", "ES256"],
            issuer=ISSUER,
            options={
                "verify_signature": True,
                "verify_exp": True,
                "verify_iat": True,
                "verify_aud": False,  # We'll check audience separately
            },
            leeway=TOKEN_LEEWAY
        )
        if timings is not None:
            timings["bearer_decode_ms"] = (time.perf_counter() - t1) * 1000

        return claims

    except jwt.ExpiredSignatureError:
        raise VerificationError("Token has expired")
    except jwt.InvalidTokenError as e:
        raise VerificationError(f"Invalid token: {str(e)}")
    except Exception as e:
        raise VerificationError(f"Token verification failed: {str(e)}")


def verify_audience(claims: Dict[str, Any]) -> bool:
    """
    Verify the audience claim matches expected value.

    Args:
        claims: Decoded token claims

    Returns:
        True if audience is valid

    Raises:
        AudienceError: If audience validation fails
    """
    aud = claims.get("aud")

    if not aud:
        raise AudienceError("Missing audience claim")

    # Handle both string and list audience claims
    if isinstance(aud, str):
        audiences = [aud]
    else:
        audiences = aud

    if EXPECTED_AUDIENCE not in audiences:
        raise AudienceError(
            f"Invalid audience. Expected '{EXPECTED_AUDIENCE}', got {audiences}"
        )

    return True


def compute_access_token_hash(access_token: str) -> str:
    """
    Compute SHA-256 hash of access token for DPoP ath claim.

    Args:
        access_token: The access token string

    Returns:
        Base64url-encoded SHA-256 hash
    """
    hash_bytes = hashlib.sha256(access_token.encode('ascii')).digest()
    return base64.urlsafe_b64encode(hash_bytes).decode('ascii').rstrip('=')


def compute_jwk_thumbprint(jwk: Dict[str, Any]) -> str:
    """
    Compute JWK thumbprint per RFC 7638.

    Args:
        jwk: JSON Web Key dictionary

    Returns:
        Base64url-encoded SHA-256 thumbprint
    """
    # Extract required members for ES256 (ECDSA P-256)
    if jwk.get("kty") == "EC":
        required = {
            "crv": jwk["crv"],
            "kty": jwk["kty"],
            "x": jwk["x"],
            "y": jwk["y"]
        }
    elif jwk.get("kty") == "RSA":
        required = {
            "e": jwk["e"],
            "kty": jwk["kty"],
            "n": jwk["n"]
        }
    else:
        raise DPoPError(f"Unsupported key type: {jwk.get('kty')}")

    # Create canonical JSON representation
    canonical = json.dumps(required, sort_keys=True, separators=(',', ':'))

    # Compute SHA-256 hash
    hash_bytes = hashlib.sha256(canonical.encode('utf-8')).digest()

    # Base64url encode
    return base64.urlsafe_b64encode(hash_bytes).decode('ascii').rstrip('=')


def check_replay_cache(jti: str) -> None:
    """
    Check if a JTI has been used before (replay detection). Fail-closed.

    Raises:
        ReplayAttackError: jti was already seen within the cache TTL.
        ReplayCacheUnavailableError: Redis is unreachable or the lookup raised
            an exception. The DPoP request must be rejected because the
            security guarantee cannot be confirmed.
    """
    if redis_client is None:
        raise ReplayCacheUnavailableError(
            "Replay cache (Redis) not initialized; cannot verify proof freshness"
        )

    try:
        key = f"replay:jti:{jti}"

        if redis_client.exists(key):
            raise ReplayAttackError(
                f"Replay attack detected: JTI '{jti}' has been used before"
            )

        # Store JTI with TTL so subsequent presentations are rejected.
        redis_client.setex(key, REPLAY_CACHE_TTL, str(time.time()))

    except (ReplayAttackError, ReplayCacheUnavailableError):
        raise
    except Exception as e:
        # Any other Redis-side failure is treated as cache-unavailable.
        # Fail-closed: the request is rejected. We log so operators can
        # distinguish a Redis incident from an actual replay attempt.
        raise ReplayCacheUnavailableError(
            f"Replay cache check failed: {e}"
        ) from e


def verify_dpop_proof(
    dpop_proof: str,
    access_token: str,
    access_token_claims: Dict[str, Any],
    method: str,
    url: str,
    timings: Optional[Dict[str, float]] = None,
) -> Tuple[bool, Optional[str]]:
    """
    Verify DPoP proof JWT per RFC 9449.

    Args:
        dpop_proof: DPoP proof JWT string
        access_token: Access token string (for ath validation)
        access_token_claims: Decoded access token claims (for cnf.jkt validation)
        method: HTTP method (e.g., "POST")
        url: Full request URL

    Returns:
        Tuple of (success: bool, error_message: Optional[str])
    """
    try:
        # Decode DPoP proof header to get JWK
        header = jwt.get_unverified_header(dpop_proof)

        # Verify typ header
        if header.get("typ") != "dpop+jwt":
            return False, "Invalid DPoP proof type header"

        # Verify algorithm
        if header.get("alg") not in ["ES256", "RS256"]:
            return False, f"Unsupported DPoP algorithm: {header.get('alg')}"

        # Extract public key from jwk claim
        jwk = header.get("jwk")
        if not jwk:
            return False, "Missing jwk in DPoP proof header"

        # Compute JWK thumbprint
        jkt = compute_jwk_thumbprint(jwk)

        # Verify sender constraint: check cnf.jkt in access token
        cnf = access_token_claims.get("cnf", {})
        expected_jkt = cnf.get("jkt")

        if not expected_jkt:
            return False, "Access token is not sender-constrained (missing cnf.jkt)"

        if jkt != expected_jkt:
            return False, "DPoP proof key does not match access token binding"

        # Convert JWK to public key for signature verification
        from jwt.algorithms import ECAlgorithm, RSAAlgorithm

        if jwk.get("kty") == "EC":
            public_key = ECAlgorithm.from_jwk(json.dumps(jwk))
        elif jwk.get("kty") == "RSA":
            public_key = RSAAlgorithm.from_jwk(json.dumps(jwk))
        else:
            return False, f"Unsupported key type: {jwk.get('kty')}"

        # Decode and verify DPoP proof JWT.
        # Per RFC 9449 §4.3 we require exp to be present and not yet expired.
        # PyJWT will raise ExpiredSignatureError on stale proofs; iat is also
        # required so we get a useful error message if absent.
        t_sig = time.perf_counter()
        proof_claims = jwt.decode(
            dpop_proof,
            public_key,
            algorithms=[header.get("alg")],
            options={
                "verify_signature": True,
                "verify_exp": True,
                "verify_iat": True,
                "require": ["jti", "htm", "htu", "iat", "exp"],
            },
            leeway=TOKEN_LEEWAY
        )
        if timings is not None:
            timings["dpop_sig_ms"] = (time.perf_counter() - t_sig) * 1000

        # Verify required claims
        jti = proof_claims.get("jti")
        if not jti:
            return False, "Missing jti claim in DPoP proof"

        htm = proof_claims.get("htm")
        if htm != method:
            return False, f"Invalid htm claim. Expected '{method}', got '{htm}'"

        htu = proof_claims.get("htu")
        if normalize_htu(htu or "") != normalize_htu(url):
            return False, f"Invalid htu claim. Expected '{url}', got '{htu}'"

        # Verify ath (access token hash)
        ath = proof_claims.get("ath")
        if not ath:
            return False, "Missing ath claim in DPoP proof"

        expected_ath = compute_access_token_hash(access_token)
        if ath != expected_ath:
            return False, "Invalid ath claim (access token hash mismatch)"

        # Check iat freshness. The window defaults to 60 seconds (the
        # standard DPoP replay-resistance budget) but is widened to
        # accommodate testbeds with unsynchronized clocks; in production
        # the default is what protects against pre-computed proofs sitting
        # in capture queues. See api/config.py for the env override.
        iat = proof_claims.get("iat")
        if iat:
            now = time.time()
            if abs(now - iat) > DPOP_FRESHNESS_WINDOW_SECONDS:
                return False, f"DPoP proof is too old or from future (iat: {iat}, now: {now})"

        # Check replay cache (raises ReplayAttackError or
        # ReplayCacheUnavailableError; both propagate to the API layer).
        t_redis = time.perf_counter()
        check_replay_cache(jti)
        if timings is not None:
            timings["redis_ms"] = (time.perf_counter() - t_redis) * 1000

        return True, None

    except (ReplayAttackError, ReplayCacheUnavailableError):
        raise  # Propagate replay/availability errors with their specific HTTP mapping
    except Exception as e:
        return False, f"DPoP verification failed: {str(e)}"


def verify_request(
    authorization_header: Optional[str],
    dpop_header: Optional[str],
    method: str,
    url: str,
    timings: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """
    Verify an incoming API request with bearer token and optional DPoP proof.

    Args:
        authorization_header: Authorization header value (e.g., "Bearer {token}")
        dpop_header: DPoP header value (DPoP proof JWT)
        method: HTTP method
        url: Request URL

    Returns:
        Dictionary with verification results and claims

    Raises:
        VerificationError: If verification fails
    """
    # Extract bearer token
    if not authorization_header:
        raise VerificationError("Missing Authorization header")

    parts = authorization_header.split()
    if len(parts) != 2:
        raise VerificationError("Invalid Authorization header format")

    auth_scheme = parts[0].lower()
    if auth_scheme not in ("bearer", "dpop"):
        raise VerificationError("Invalid Authorization header format")

    access_token = parts[1]

    # If using DPoP scheme, DPoP header is required
    if auth_scheme == "dpop" and not dpop_header:
        raise VerificationError("DPoP scheme requires DPoP proof header")

    # Verify bearer token
    claims = verify_bearer_token(access_token, timings=timings)

    # Verify audience
    verify_audience(claims)

    # If DPoP header is present, verify DPoP proof
    dpop_verified = False
    if dpop_header:
        success, error = verify_dpop_proof(
            dpop_header,
            access_token,
            claims,
            method,
            url,
            timings=timings,
        )
        if not success:
            raise DPoPError(error)
        dpop_verified = True

    return {
        "claims": claims,
        "dpop_verified": dpop_verified,
        "subject": claims.get("sub"),
        "audience": claims.get("aud"),
        "actor": claims.get("act"),  # For delegation
    }
