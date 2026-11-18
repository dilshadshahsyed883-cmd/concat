"""Offline security gate for the independently scoped Concat Web release transport."""
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class GuardedTailnetReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = (ROOT / ".github/workflows/deploy.yml").read_text()
        cls.helper = (ROOT / "deploy/guarded-release.sh").read_text()

    def test_shell_syntax_without_host_access(self):
        completed = subprocess.run(
            ["bash", "-n", str(ROOT / "deploy/guarded-release.sh")],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_reuses_vps1_private_transport_with_pins(self):
        wf = self.workflow
        for marker in (
            "tailscale/github-action@v4", "oauth-client-id:",
            "secrets.TS_OAUTH_CLIENT_ID", "secrets.TS_AUDIENCE",
            "tags: tag:ci", "id-token: write",
            "secrets.VPS1_SSH_PRIVATE_KEY",
            "SHA256:oeX4vWPvVspKOTrvbMgUa+uWPGyXLz3531TQEy5wp1E",
            "StrictHostKeyChecking=yes", "UpdateHostKeys=no",
            "vps1deploy@100.87.55.92",
        ):
            self.assertIn(marker, wf)
        for unsafe in ("ssh-keyscan", "StrictHostKeyChecking=no",
                       "sudo -n bash -s", "/usr/local/sbin/vps1-production-deploy"):
            self.assertNotIn(unsafe, wf)

    def test_automatic_release_is_explicitly_opted_in(self):
        wf = self.workflow
        for marker in (
            "push:", "branches: [main]", "workflow_dispatch:",
            "github.actor == 'dilshadshahsyed883-cmd'",
            "vars.CONCAT_WEB_DEPLOY_ENABLED == 'true'",
            "vars.CONCAT_WEB_AUTO_DEPLOY_ENABLED == 'true'",
            "python -m unittest discover -s tests -p 'test_admin_security.py'",
            "environment: concat-web-production",
            "github.ref == 'refs/heads/main'",
        ):
            self.assertIn(marker, wf)
        self.assertLess(wf.index("Focused build, integration and negative security checks"),
                        wf.index("Activate only isolated Concat service with rollback"))

    def test_only_installed_restricted_helper_is_invoked_as_root(self):
        wf, sh = self.workflow, self.helper
        for marker in (
            "sudo -n /usr/local/sbin/concat-web-deploy preflight",
            "sudo -n /usr/local/sbin/concat-web-deploy deploy",
            "sha256sum -c -", "stat -c %U",
            "readlink -f -- \"$0\"", "root-owned",
            "concat-web.service", "CONCAT_SCENE_SSO_SECRET",
            "concat_sso_sessions", "runuser -u concatweb",
            "SWITCHED=1", "cleanup_and_rollback", "/healthz",
            "/api/v1/session", "/dashboard",
        ):
            self.assertIn(marker, wf + "\n" + sh)
        self.assertNotIn("systemctl restart scene-", sh)
        self.assertNotIn("systemctl restart vps1", sh)
        self.assertNotIn("systemctl reload nginx", sh)

    def test_no_public_cutover_and_no_bypass(self):
        self.assertNotIn("nginx -s reload", self.helper)
        self.assertNotIn("sudo -n bash -s", self.workflow)
        self.assertNotIn("ssh-keyscan", self.workflow)
        self.assertTrue(re.search(r"\b401\b", self.helper))
        self.assertIn("mode=ro", self.helper)
        self.assertIn("if [[ \"$MODE\" == preflight ]]", self.helper)


if __name__ == "__main__":
    unittest.main()
