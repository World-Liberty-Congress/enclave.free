"""Validate the rendered production fallback contract without starting services."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class ProductionComposeTests(unittest.TestCase):
    def render(self, include_maple_key):
        fixture = (ROOT / ".env.liberator.example").read_text()
        if not include_maple_key:
            fixture = "\n".join(line for line in fixture.splitlines() if not line.startswith("MAPLE_API_KEY="))
        with tempfile.TemporaryDirectory() as temp:
            env_file = Path(temp) / "fixture.env"
            env_file.write_text(fixture)
            return subprocess.run(
                ["docker", "compose", "--project-name", "liberator-contract", "--env-file", str(env_file),
                 "-f", "docker-compose.infra.yml", "-f", "docker-compose.app.yml",
                 "-f", "docker-compose.liberator-production.yml", "config", "--format", "json"],
                cwd=ROOT, env={key: os.environ[key] for key in ("PATH", "HOME") if key in os.environ},
                capture_output=True, text=True,
            )

    def test_default_production_services_include_maple_with_organization_key(self):
        result = self.render(include_maple_key=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        services = json.loads(result.stdout)["services"]
        self.assertEqual(len(services), 10)
        for name, service in services.items():
            self.assertEqual(service["logging"], {"driver": "journald", "options": {"tag": "liberator/{{.Name}}"}}, name)
        self.assertIn("maple-proxy", services)
        self.assertFalse(services["maple-proxy"].get("profiles"))
        self.assertEqual(services["sage"]["environment"]["MAPLE_API_KEY"], "REPLACE_WITH_ORGANIZATION_MAPLE_KEY")

    def test_production_rejects_missing_maple_key(self):
        result = self.render(include_maple_key=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("MAPLE_API_KEY", result.stderr)


if __name__ == "__main__":
    unittest.main()
