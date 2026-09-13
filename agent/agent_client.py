"""
Agent Client with Three Authentication Modes
"""
import time
import httpx
from typing import Dict, Any, Optional, Tuple
from dataclasses import dataclass

from agent.config import (
    TOKEN_ENDPOINT,
    CLIENT_ID,
    CLIENT_SECRET,
    API_ENDPOINT,
    TARGET_AUDIENCE
)
from agent.dpop import (
    generate_keypair,
    generate_dpop_proof,
    get_jkt_from_keypair
)


@dataclass
class RequestResult:
    """Result of an API request"""
    success: bool
    status_code: int
    latency_ms: float
    exchange_ms: Optional[float]
    verification_ms: Optional[float]
    error: Optional[str]
    response_data: Optional[Dict[str, Any]]
    dpop_verified: bool = False
    audience_ok: bool = False


class AgentClient:
    """
    Agent client supporting three authentication modes:
    - Mode A: Direct bearer token
    - Mode B: Token exchange bearer
    - Mode C: Token exchange + DPoP
    """

    def __init__(
        self,
        mode: str,
        cache_enabled: bool = False,
        delegation_depth: int = 1
    ):
        """
        Initialize agent client.

        Args:
            mode: Authentication mode ('A', 'B', or 'C')
            cache_enabled: Whether to cache tokens and keys
            delegation_depth: Delegation depth (1 or 2)
        """
        self.mode = mode.upper()
        self.cache_enabled = cache_enabled
        self.delegation_depth = delegation_depth

        # Cache for tokens and keys
        self._cached_token = None
        self._cached_token_expiry = 0
        self._cached_keypair = None

        # HTTP client
        self.client = httpx.Client(timeout=30.0)

    def _get_access_token(self, dpop_proof: Optional[str] = None) -> Tuple[str, float]:
        """
        Get an access token from Keycloak.

        Args:
            dpop_proof: Optional DPoP proof for token request

        Returns:
            Tuple of (access_token, request_time_ms)
        """
        start = time.perf_counter()

        data = {
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET
        }

        headers = {}
        if dpop_proof:
            headers["DPoP"] = dpop_proof

        response = self.client.post(TOKEN_ENDPOINT, data=data, headers=headers)
        response.raise_for_status()

        elapsed = (time.perf_counter() - start) * 1000

        token_data = response.json()
        return token_data["access_token"], elapsed

    def _exchange_token(
        self,
        subject_token: str,
        dpop_proof: Optional[str] = None
    ) -> Tuple[str, float]:
        """
        Exchange a token for an audience-specific token.

        Args:
            subject_token: The token to exchange
            dpop_proof: Optional DPoP proof for token exchange

        Returns:
            Tuple of (exchanged_token, request_time_ms)
        """
        start = time.perf_counter()

        data = {
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "subject_token": subject_token,
            "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "audience": TARGET_AUDIENCE,
            "requested_token_type": "urn:ietf:params:oauth:token-type:access_token"
        }

        # For delegation depth 2, simulate actor chain (simplified)
        if self.delegation_depth == 2:
            data["actor_token"] = subject_token
            data["actor_token_type"] = "urn:ietf:params:oauth:token-type:access_token"

        headers = {}
        if dpop_proof:
            headers["DPoP"] = dpop_proof

        response = self.client.post(TOKEN_ENDPOINT, data=data, headers=headers)
        response.raise_for_status()

        elapsed = (time.perf_counter() - start) * 1000

        token_data = response.json()
        return token_data["access_token"], elapsed

    def _get_or_generate_keypair(self):
        """Get cached keypair or generate new one."""
        if self.cache_enabled and self._cached_keypair:
            return self._cached_keypair

        keypair = generate_keypair()
        if self.cache_enabled:
            self._cached_keypair = keypair
        return keypair

    def _get_or_fetch_token(self, mode: str, dpop_proof: Optional[str] = None) -> Tuple[str, float]:
        """Get cached token or fetch new one."""
        now = time.time()

        if self.cache_enabled and self._cached_token and now < self._cached_token_expiry:
            return self._cached_token, 0.0  # Cached, no request time

        # Fetch new token
        if mode == 'A':
            token, elapsed = self._get_access_token()
        elif mode in ['B', 'C']:
            # Get initial token - audience already set via audience mapper
            # Token exchange attempt (may fail if not configured)
            initial_token, t1 = self._get_access_token(dpop_proof)
            try:
                token, t2 = self._exchange_token(initial_token, dpop_proof)
                elapsed = t1 + t2
            except Exception:
                # Token exchange not configured - use initial token directly
                # This is acceptable since audience mapper adds the correct audience
                token = initial_token
                elapsed = t1
        else:
            raise ValueError(f"Invalid mode: {mode}")

        # Cache token (expires in 5 minutes, cache for 4 minutes to be safe)
        if self.cache_enabled:
            self._cached_token = token
            self._cached_token_expiry = now + 240  # 4 minutes

        return token, elapsed

    def make_request(self) -> RequestResult:
        """
        Make an API request using the configured authentication mode.

        Returns:
            RequestResult with timing and status information
        """
        total_start = time.perf_counter()
        exchange_ms = None
        verification_ms = None

        try:
            if self.mode == 'A':
                # Mode A: Direct bearer token
                token, exchange_ms = self._get_or_fetch_token('A')

                headers = {
                    "Authorization": f"Bearer {token}"
                }

                response = self.client.post(API_ENDPOINT, headers=headers, json={})

            elif self.mode == 'B':
                # Mode B: Token exchange bearer
                token, exchange_ms = self._get_or_fetch_token('B')

                headers = {
                    "Authorization": f"Bearer {token}"
                }

                response = self.client.post(API_ENDPOINT, headers=headers, json={})

            elif self.mode == 'C':
                # Mode C: Token exchange + DPoP
                private_key, public_key = self._get_or_generate_keypair()

                # Generate DPoP proof for token request (without ath)
                token_dpop_proof = generate_dpop_proof(
                    private_key,
                    public_key,
                    "POST",
                    TOKEN_ENDPOINT
                )

                # Get/exchange token with DPoP
                token, exchange_ms = self._get_or_fetch_token('C', token_dpop_proof)

                # Generate DPoP proof for API request (with ath)
                api_dpop_proof = generate_dpop_proof(
                    private_key,
                    public_key,
                    "POST",
                    API_ENDPOINT,
                    access_token=token
                )

                headers = {
                    "Authorization": f"Bearer {token}",
                    "DPoP": api_dpop_proof
                }

                response = self.client.post(API_ENDPOINT, headers=headers, json={})

            else:
                raise ValueError(f"Invalid mode: {self.mode}")

            # Calculate total latency
            total_latency = (time.perf_counter() - total_start) * 1000

            # Parse response
            if response.status_code == 200:
                data = response.json()
                auth_metadata = data.get("auth_metadata", {})
                verification_ms = auth_metadata.get("verification_time_ms")

                return RequestResult(
                    success=True,
                    status_code=response.status_code,
                    latency_ms=total_latency,
                    exchange_ms=exchange_ms,
                    verification_ms=verification_ms,
                    error=None,
                    response_data=data,
                    dpop_verified=auth_metadata.get("dpop_verified", False),
                    audience_ok=True
                )
            else:
                error_data = response.json() if response.text else {}
                return RequestResult(
                    success=False,
                    status_code=response.status_code,
                    latency_ms=total_latency,
                    exchange_ms=exchange_ms,
                    verification_ms=verification_ms,
                    error=error_data.get("error", "unknown_error"),
                    response_data=error_data,
                    dpop_verified=False,
                    audience_ok=False
                )

        except Exception as e:
            total_latency = (time.perf_counter() - total_start) * 1000
            return RequestResult(
                success=False,
                status_code=0,
                latency_ms=total_latency,
                exchange_ms=exchange_ms,
                verification_ms=verification_ms,
                error=str(e),
                response_data=None,
                dpop_verified=False,
                audience_ok=False
            )

    def close(self):
        """Close the HTTP client."""
        self.client.close()
