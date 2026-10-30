from __future__ import annotations

"""Rate limiting helpers for FastAPI dependencies."""

from datetime import datetime, timedelta
from collections import defaultdict
import hashlib
import logging
import os
import time
from typing import Callable, Optional, Union
from fastapi import Request, HTTPException

logger = logging.getLogger("enclave.rate_limit")

_VALKEY_CLIENT = None
_VALKEY_IMPORT_ERROR: Exception | None = None


def _rate_limit_backend() -> str:
    return os.getenv("RATE_LIMIT_BACKEND", "memory").strip().lower() or "memory"


def _valkey_url() -> str:
    return os.getenv("RATE_LIMIT_VALKEY_URL", "redis://valkey:6379/0").strip()


def _valkey_client():
    """Return a shared Redis-protocol client for self-hosted Valkey."""
    global _VALKEY_CLIENT, _VALKEY_IMPORT_ERROR
    if _VALKEY_CLIENT is not None:
        return _VALKEY_CLIENT
    try:
        import redis.asyncio as redis
    except Exception as exc:  # pragma: no cover - exercised when dependency is absent
        _VALKEY_IMPORT_ERROR = exc
        raise RuntimeError("redis package is required for Valkey-backed rate limiting") from exc

    _VALKEY_CLIENT = redis.from_url(
        _valkey_url(),
        encoding="utf-8",
        decode_responses=True,
        socket_connect_timeout=1.0,
        socket_timeout=1.0,
    )
    return _VALKEY_CLIENT


def _safe_key_part(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def rate_limit_backend_status() -> dict:
    """Operator-facing health summary for the configured Shared Rate Limit Store."""
    backend = _rate_limit_backend()
    if backend == "memory":
        return {
            "backend": "memory",
            "status": "local_only",
            "summary": "In-memory rate limiting is active for this process only.",
        }
    if backend != "valkey":
        return {
            "backend": backend,
            "status": "unsupported",
            "summary": "Unsupported RATE_LIMIT_BACKEND value.",
        }
    try:
        client = _valkey_client()
        pong = await client.ping()
        return {
            "backend": "valkey",
            "status": "healthy" if pong else "unhealthy",
            "summary": "Self-hosted Valkey is coordinating rate limits across runtime instances.",
        }
    except Exception as exc:
        logger.exception("Valkey backend health check failed")
        return {
            "backend": "valkey",
            "status": "unhealthy",
            "summary": "error checking valkey backend health",
        }


class RateLimiter:
    """
    Rate limiter with in-memory local mode and Valkey shared mode.

    Usage:
        limiter = RateLimiter(limit=5, window_seconds=60)

        @app.post("/endpoint")
        async def endpoint(request: Request, _: None = Depends(limiter)):
            ...
    """

    def __init__(
        self,
        limit: Union[int, Callable[[], int]],
        window_seconds: int,
        key_func: Optional[Callable[[Request], str]] = None,
        *,
        namespace: str = "",
    ):
        if window_seconds <= 0:
            raise ValueError("window_seconds must be a positive number")
        self._limit = limit
        self.window = timedelta(seconds=window_seconds)
        self.window_seconds = window_seconds
        self.namespace = namespace
        self.key_func = key_func or (lambda r: r.client.host if r.client else "unknown")
        self.requests: dict[str, list[datetime]] = defaultdict(list)
        self._last_cleanup = datetime.utcnow()

    def _current_limit(self) -> int:
        raw = self._limit() if callable(self._limit) else self._limit
        try:
            value = int(raw)
        except (ValueError, TypeError):
            value = 0
        return max(0, value)

    def _cleanup_if_needed(self) -> None:
        """Remove expired entries periodically (every 60 seconds)."""
        now = datetime.utcnow()
        if now - self._last_cleanup < timedelta(seconds=60):
            return

        self._last_cleanup = now
        cutoff = now - self.window

        # Remove expired timestamps
        expired_keys = []
        for key, timestamps in self.requests.items():
            self.requests[key] = [t for t in timestamps if t > cutoff]
            if not self.requests[key]:
                expired_keys.append(key)

        # Remove empty keys
        for key in expired_keys:
            del self.requests[key]

    def _raise_rate_limited(self) -> None:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded. Try again in {self.window_seconds} seconds.",
            headers={"Retry-After": str(self.window_seconds)},
        )

    async def _check_valkey(self, request: Request, limit: int) -> None:
        key = self.key_func(request)
        bucket = int(time.time() // self.window_seconds)
        prefix = f"{self.namespace}:" if self.namespace else ""
        valkey_key = f"enclave:rate-limit:{prefix}{self.window_seconds}:{bucket}:{_safe_key_part(key)}"
        try:
            client = _valkey_client()
            count = await client.incr(valkey_key)
            if count == 1:
                await client.expire(valkey_key, self.window_seconds + 1)
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning("Valkey rate limit check failed: %s", exc)
            raise HTTPException(
                status_code=503,
                detail="Shared rate limit store is unavailable."
            ) from exc

        if int(count) > limit:
            self._raise_rate_limited()

    async def _check_memory(self, request: Request, limit: int) -> None:
        self._cleanup_if_needed()

        key = self.key_func(request)
        now = datetime.utcnow()
        cutoff = now - self.window

        # Filter to recent requests only
        self.requests[key] = [t for t in self.requests[key] if t > cutoff]

        if limit <= 0:
            raise HTTPException(
                status_code=429,
                detail="Rate limit exceeded. Try again later."
            )

        if len(self.requests[key]) >= limit:
            self._raise_rate_limited()

        self.requests[key].append(now)

    async def __call__(self, request: Request) -> None:
        """FastAPI dependency - raises 429 if rate limit exceeded."""
        limit = self._current_limit()
        if limit <= 0:
            raise HTTPException(
                status_code=429,
                detail="Rate limit exceeded. Try again later."
            )

        backend = _rate_limit_backend()
        if backend == "valkey":
            await self._check_valkey(request, limit)
            return
        if backend != "memory":
            raise HTTPException(
                status_code=503,
                detail="Unsupported rate limit backend."
            )
        await self._check_memory(request, limit)
