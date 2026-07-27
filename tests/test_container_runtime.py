from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ContainerConfigurationTest(unittest.TestCase):
    def test_image_uses_init_and_direct_virtualenv_entrypoint(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text()

        self.assertIn("nodejs npm tini", dockerfile)
        self.assertIn('ENTRYPOINT ["/usr/bin/tini", "-g", "--"]', dockerfile)
        self.assertIn('CMD ["/app/.venv/bin/ai-limit-monitor", "run"]', dockerfile)
        self.assertNotIn('CMD ["uv", "run"', dockerfile)

    def test_healthcheck_bypasses_uv_and_has_a_short_timeout(self) -> None:
        compose = (ROOT / "docker-compose.yml").read_text()

        self.assertIn("CMD-SHELL", compose)
        self.assertIn("/proc/1/task/1/children", compose)
        self.assertIn("grep -aq '/app/.venv/bin/ai-limit-monitor'", compose)
        self.assertIn("timeout: 3s", compose)
        self.assertNotIn('test: ["CMD", "uv", "run"', compose)


if __name__ == "__main__":
    unittest.main()
