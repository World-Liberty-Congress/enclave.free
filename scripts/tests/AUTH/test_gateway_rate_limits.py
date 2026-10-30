"""Real Nginx regression fixture. Requires Docker; no real auth requests or emails.

Run from the repository root: python3 scripts/tests/AUTH/test_gateway_rate_limits.py
"""
import ipaddress
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[3]
IMAGE = "nginx:1.27-alpine"


def docker(*args, check=True):
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check).stdout.strip()


class GatewayRateLimitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.name = "signin-test-" + uuid.uuid4().hex[:8]
        cls.upstream, cls.gateway, cls.client = [cls.name + suffix for suffix in ["-upstream", "-gateway", "-client"]]
        cls.temporary = tempfile.TemporaryDirectory(prefix="signin-test-")
        cls.addClassCleanup(cls.cleanup)
        directory = Path(cls.temporary.name)
        stub = directory / "upstream.conf"
        stub.write_text('events {}\nhttp { server { listen 18000; location / { default_type application/json; return 200 \'{"client":"$http_x_forwarded_for"}\'; } } }\n')
        trusted = directory / "trusted"
        trusted.mkdir()
        docker("network", "create", cls.name)
        docker("run", "-d", "--name", cls.upstream, "--network", cls.name,
               "--network-alias", "core-backend", "--network-alias", "sage",
               "--mount", f"type=bind,src={stub},dst=/etc/nginx/nginx.conf,readonly", IMAGE)
        docker("run", "-d", "--name", cls.gateway, "--network", cls.name,
               "-p", "127.0.0.1::18000", "--mount",
               f"type=bind,src={ROOT / 'gateway/nginx.conf'},dst=/etc/nginx/nginx.conf,readonly",
               "--mount", f"type=bind,src={trusted},dst=/etc/nginx/trusted-proxies,readonly", IMAGE)
        for _ in range(60):
            container = json.loads(docker("inspect", cls.gateway))[0]
            ports = container["NetworkSettings"]["Ports"].get("18000/tcp")
            if ports:
                cls.port = ports[0]["HostPort"]
                try:
                    status, _, body = cls.request("/health", "198.51.100.1", "GET")
                    if status == 200:
                        peer = json.loads(body)["client"]
                        break
                except OSError:
                    pass
            if container["State"]["Status"] == "exited":
                raise RuntimeError(docker("logs", cls.gateway, check=False))
            time.sleep(0.1)
        else:
            raise RuntimeError("Gateway did not start")
        ipaddress.ip_address(peer)
        (trusted / "edge.conf").write_text(f"set_real_ip_from {peer};\nreal_ip_header X-Forwarded-For;\nreal_ip_recursive on;\n")
        docker("exec", cls.gateway, "nginx", "-t")
        docker("exec", cls.gateway, "nginx", "-s", "reload")
        for _ in range(60):
            if json.loads(cls.request("/health", "198.51.100.1", "GET")[2])["client"] == "198.51.100.1":
                break
            time.sleep(0.05)
        else:
            raise RuntimeError("Trusted edge configuration did not load")
        docker("run", "-d", "--name", cls.client, "--network", cls.name, IMAGE)

    @classmethod
    def cleanup(cls):
        docker("rm", "-f", cls.gateway, cls.upstream, cls.client, check=False)
        docker("network", "rm", cls.name, check=False)
        cls.temporary.cleanup()

    @classmethod
    def request(cls, path, client, method="POST"):
        request = urllib.request.Request(f"http://127.0.0.1:{cls.port}{path}",
            data=b"{}" if method == "POST" else None, method=method,
            headers={"X-Forwarded-For": client, "Content-Type": "application/json"})
        try:
            response = urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, response.headers, response.read().decode()

    def test_fresh_client_is_independent_and_same_client_is_limited(self):
        for _ in range(6):
            self.assertEqual(self.request("/auth/magic-link", "198.51.100.10")[0], 200)
        status, headers, body = self.request("/auth/magic-link", "198.51.100.10")
        self.assertEqual(status, 429)
        self.assertEqual(headers["Retry-After"], "60")
        self.assertEqual(headers.get_content_type(), "application/json")
        self.assertIsInstance(json.loads(body)["detail"], str)
        self.assertEqual(self.request("/auth/magic-link", "198.51.100.20")[0], 200)

    def test_requesting_links_does_not_exhaust_verification_or_admin_auth(self):
        for _ in range(6):
            self.assertEqual(self.request("/auth/magic-link", "198.51.100.30")[0], 200)
        self.assertEqual(self.request("/auth/verify", "198.51.100.30")[0], 200)
        self.assertEqual(self.request("/admin/auth", "198.51.100.30")[0], 200)

    def test_untrusted_peer_cannot_forge_the_forwarded_client(self):
        body = docker("exec", self.client, "wget", "-qO-", "--header", "X-Forwarded-For: 198.51.100.99",
                      f"http://{self.gateway}:18000/health")
        observed = json.loads(body)["client"]
        self.assertNotEqual(observed, "198.51.100.99")
        ipaddress.ip_address(observed)
        for i in range(7):
            result = subprocess.run(["docker", "exec", self.client, "wget", "-qO-", "--server-response",
                "--post-data", "{}", "--header", f"X-Forwarded-For: 198.51.100.{100+i}",
                f"http://{self.gateway}:18000/auth/magic-link"], capture_output=True, text=True)
            self.assertIn("200 OK" if i < 6 else "429 Too Many Requests", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
