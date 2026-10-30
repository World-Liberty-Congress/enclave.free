"""Exercise the real ASGI proxy boundary without sending email or using production Valkey."""
import asyncio
import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException, Request
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from backend.tests.test_rate_limit import FakeValkey


class ProxyRateLimitTest(unittest.TestCase):
    def scenario(self, trusted, forwarded):
        import rate_limit
        module = importlib.reload(rate_limit)
        store = FakeValkey()
        limiter = module.RateLimiter(limit=5, window_seconds=60, namespace="magic-link")
        statuses = []

        async def app(scope, receive, send):
            try:
                await limiter(Request(scope))
                statuses.append(200)
            except HTTPException as error:
                statuses.append(error.status_code)

        async def run():
            middleware = ProxyHeadersMiddleware(app, trusted_hosts=trusted)
            for address in forwarded:
                scope = {"type": "http", "method": "POST", "path": "/auth/magic-link",
                         "scheme": "http", "client": ("192.0.2.10", 12345),
                         "headers": [(b"x-forwarded-for", address.encode())]}
                await middleware(scope, None, None)

        with patch.dict(os.environ, {"RATE_LIMIT_BACKEND": "valkey"}), \
                patch.object(module, "_valkey_client", return_value=store), \
                patch.object(module.time, "time", return_value=1700000040):
            asyncio.run(run())
        return statuses, len(store.counts)

    def test_trusted_gateway_keeps_visitors_independent(self):
        statuses, counters = self.scenario(["192.0.2.10"], ["198.51.100.1"] * 5 + ["198.51.100.2"])
        self.assertEqual(statuses, [200] * 6)
        self.assertEqual(counters, 2)

    def test_untrusted_peer_cannot_rotate_forwarded_headers(self):
        statuses, counters = self.scenario(["127.0.0.1"], [f"198.51.100.{i}" for i in range(1, 7)])
        self.assertEqual(statuses, [200] * 5 + [429])
        self.assertEqual(counters, 1)

    def test_trusted_same_client_still_has_a_limit(self):
        statuses, counters = self.scenario(["192.0.2.10"], ["198.51.100.1"] * 6)
        self.assertEqual(statuses, [200] * 5 + [429])
        self.assertEqual(counters, 1)


if __name__ == "__main__":
    unittest.main()
