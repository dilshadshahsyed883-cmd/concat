"""Release policy: Concat local-admin only; all production changes via VPS terminal."""
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class LocalAdminManualReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helper = (ROOT / "deploy/guarded-release.sh").read_text()
        cls.workflow = (ROOT / ".github/workflows/deploy.yml").read_text()
        cls.admin = (ROOT / "app/modules/admin/routes.py").read_text()
        cls.auth = (ROOT / "app/modules/admin/security.py").read_text()
        cls.bootstrap = (ROOT / "deploy/init-local-admin.py").read_text()

    def test_shell_syntax(self):
        result = subprocess.run(["bash", "-n", str(ROOT / "deploy/guarded-release.sh")],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_local_admin_has_no_scene_dependency(self):
        for item in (self.helper, self.admin, self.auth, self.bootstrap):
            for forbidden in ("CONCAT_SCENE_USERS_DB", "CONCAT_SCENE_SSO_SECRET",
                              "concat_sso_sessions", "scene_sid_hash",
                              "/concat-sso/authorize"):
                self.assertNotIn(forbidden, item)
        self.assertNotIn('"/auth/scene/start"', self.admin)
        self.assertIn('"/auth/login"', self.admin)
        self.assertIn("CONCAT_ADMIN_PASSWORD_HASH", self.helper)
        self.assertIn("CONCAT_AUTH_MODE", self.helper)
        self.assertIn("CONCAT_ADMIN_SESSION_SECRET", self.helper)

    def test_release_guard_uses_only_concat_service(self):
        helper = self.helper
        for item in ("sha256sum -c -", "root-owned", "concat-web.service",
                     "runuser -u concatweb", "/healthz", "/api/v1/session",
                     "cleanup_and_rollback", "/etc/concat-web/concat-web.env"):
            self.assertIn(item, helper)
        for forbidden in ("systemctl restart vps1", "systemctl reload nginx",
                          "/usr/local/sbin/vps1-production-deploy",
                          "systemctl restart scene-", "sudo -n bash -s"):
            self.assertNotIn(forbidden, helper)
        self.assertIn('if [[ "$MODE" == preflight ]]', helper)

    def test_github_actions_cannot_deploy_automatically(self):
        wf = self.workflow
        self.assertIn("workflow_dispatch", wf)
        for forbidden in ("push:", "ssh ", "scp ", "tailscale/",
                          "VPS1_SSH_PRIVATE_KEY", "CONCAT_WEB_AUTO_DEPLOY_ENABLED",
                          "concat-web-deploy deploy"):
            self.assertNotIn(forbidden, wf)
        self.assertIn("python -m unittest discover -s tests -p 'test_admin_security.py'", wf)

    def test_password_hash_bootstrap_is_one_time_and_private(self):
        source = self.bootstrap
        for required in ("getpass.getpass", "hashlib.scrypt", "O_EXCL",
                         "O_NOFOLLOW", "0o600", "SECRETS=NOT_DISPLAYED"):
            self.assertIn(required, source)
        self.assertNotIn("print(password)", source)


if __name__ == "__main__":
    unittest.main()
