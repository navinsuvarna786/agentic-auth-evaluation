"""
Protected Internal API Service (FastAPI)
"""
from fastapi import FastAPI, Header, Request, HTTPException
from fastapi.responses import JSONResponse
from typing import Optional
import time
import logging
import os
import json
import threading

from api.verifier import (
    verify_request,
    VerificationError,
    ReplayAttackError,
    ReplayCacheUnavailableError,
    AudienceError,
    DPoPError
)
from api.config import API_HOST, API_PORT

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Optional per-stage timing log. When STAGE_LOG_PATH is set, each
# successful Mode-C request appends one JSON line with the
# decomposition (jwks_ms, bearer_decode_ms, dpop_sig_ms, redis_ms,
# total verification_ms). Used to attribute the bimodal-cluster tail
# during stress runs. Disabled when env var is unset (default).
_STAGE_LOG_PATH = os.getenv("STAGE_LOG_PATH")
_STAGE_LOG_LOCK = threading.Lock()


def _maybe_log_stages(timings: dict, total_ms: float, dpop_verified: bool) -> None:
    if not _STAGE_LOG_PATH or not timings:
        return
    record = dict(timings)
    record["verification_ms"] = round(total_ms, 3)
    record["dpop_verified"] = dpop_verified
    record["ts"] = time.time()
    line = json.dumps(record) + "\n"
    with _STAGE_LOG_LOCK:
        with open(_STAGE_LOG_PATH, "a") as f:
            f.write(line)

# Create FastAPI application
app = FastAPI(
    title="Protected Internal API",
    description="Internal service with layered authentication support",
    version="1.0.0"
)


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "timestamp": time.time()}


@app.post("/api/task")
async def execute_task(
    request: Request,
    authorization: Optional[str] = Header(None),
    dpop: Optional[str] = Header(None)
):
    """
    Protected API endpoint that requires authentication.
    
    Supports:
    - Mode A: Bearer token only
    - Mode B: Bearer token with audience validation
    - Mode C: Bearer token + DPoP proof with replay protection
    
    Headers:
        Authorization: Bearer {access_token}
        DPoP: {dpop_proof_jwt} (optional, for Mode C)
    
    Returns:
        Success response with task result and authentication metadata
    """
    start_time = time.perf_counter()
    
    try:
        # Get request URL for DPoP verification
        url = str(request.url)
        method = request.method
        
        # Verify request (bearer token + optional DPoP)
        stage_timings: dict = {}
        verification_start = time.perf_counter()
        result = verify_request(
            authorization_header=authorization,
            dpop_header=dpop,
            method=method,
            url=url,
            timings=stage_timings if _STAGE_LOG_PATH else None,
        )
        verification_time = (time.perf_counter() - verification_start) * 1000
        _maybe_log_stages(stage_timings, verification_time, result.get("dpop_verified", False))
        
        # Extract claims
        claims = result["claims"]
        dpop_verified = result["dpop_verified"]
        
        # Log successful authentication
        logger.info(
            f"Authentication successful - "
            f"subject={result['subject']}, "
            f"audience={result['audience']}, "
            f"dpop={dpop_verified}, "
            f"verification_time={verification_time:.2f}ms"
        )
        
        # Simulate task execution
        task_result = {
            "status": "success",
            "message": "Task executed successfully",
            "data": {
                "task_id": f"task-{int(time.time() * 1000)}",
                "processed_at": time.time()
            }
        }
        
        # Prepare response with authentication metadata
        total_time = (time.perf_counter() - start_time) * 1000
        
        response = {
            "result": task_result,
            "auth_metadata": {
                "subject": result["subject"],
                "audience": result["audience"],
                "actor": result.get("actor"),
                "dpop_verified": dpop_verified,
                "verification_time_ms": round(verification_time, 2),
                "total_time_ms": round(total_time, 2)
            }
        }
        
        return JSONResponse(content=response, status_code=200)
        
    except ReplayAttackError as e:
        logger.warning(f"Replay attack detected: {str(e)}")
        return JSONResponse(
            content={
                "error": "replay_attack",
                "message": str(e)
            },
            status_code=403
        )

    except ReplayCacheUnavailableError as e:
        # Fail-closed: replay protection cannot be confirmed → reject the
        # request with 503 so callers and operators can distinguish this
        # from an actual replay (403) or auth failure (401).
        logger.error(f"Replay cache unavailable: {str(e)}")
        return JSONResponse(
            content={
                "error": "replay_cache_unavailable",
                "message": "Replay protection is temporarily unavailable; "
                           "request rejected to preserve security posture."
            },
            status_code=503
        )

    except AudienceError as e:
        logger.warning(f"Audience validation failed: {str(e)}")
        return JSONResponse(
            content={
                "error": "invalid_audience",
                "message": str(e)
            },
            status_code=403
        )
        
    except DPoPError as e:
        logger.warning(f"DPoP verification failed: {str(e)}")
        return JSONResponse(
            content={
                "error": "invalid_dpop",
                "message": str(e)
            },
            status_code=401
        )
        
    except VerificationError as e:
        logger.warning(f"Authentication failed: {str(e)}")
        return JSONResponse(
            content={
                "error": "authentication_failed",
                "message": str(e)
            },
            status_code=401
        )
        
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}", exc_info=True)
        return JSONResponse(
            content={
                "error": "internal_error",
                "message": "An unexpected error occurred"
            },
            status_code=500
        )


@app.get("/api/info")
async def api_info():
    """
    Public endpoint providing API information.
    """
    return {
        "name": "Protected Internal API",
        "version": "1.0.0",
        "authentication_modes": {
            "mode_a": "Bearer token only",
            "mode_b": "Bearer token with audience validation (token exchange)",
            "mode_c": "Bearer token + DPoP proof (sender-constrained)"
        },
        "endpoints": {
            "/api/task": "Protected endpoint requiring authentication",
            "/api/info": "Public information endpoint",
            "/health": "Health check endpoint"
        }
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=API_HOST, port=API_PORT)
