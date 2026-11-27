"""Release policy: Concat local-admin only; all production changes via VPS terminal."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class LocalAdminManualReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helper = (ROOT / "deploy/guarded-release.sh").read_text()
        cls.workflow = (ROOT.parent / ".github/workflows/concat-web-deploy.yml").read_text()
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


    def test_manual_main_stage_is_sha_pinned_and_concat_only(self):
        script = (ROOT / "deploy/manual-integration-release.sh").read_text()
        result = subprocess.run(
            ["bash", "-n", str(ROOT / "deploy/manual-integration-release.sh")],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        for marker in (
            "REPO='https://github.com/dilshadshahsyed883-cmd/concat.git'",
            '-C "$WORK/source/concat-web"',
            '$WORK/source/concat-web/deploy/guarded-release.sh',
            "BRANCH='main'", 
            "CONCAT_MAIN_PREFLIGHT=PASS",
            'refs/remotes/origin/$BRANCH',
            'verify_web_release "$WORK/source" "$SHA" "$REMOTE"',
            'checkout --quiet --detach "$SHA"',
            '[[ "$approved_tree" == "$main_tree" ]]',
            ':(glob).github/workflows/concat-web-*.yml',
            'git -C "$WORK/source" rev-parse HEAD',
            "CONCAT_SQLITE_BACKUP=PASS",
            "deploy/init-local-admin.py",
            "concat-web-deploy preflight",
            'concat-web-deploy deploy "$SHA"',
            "DEPLOY_COMPLETED=1",
            "trap cleanup EXIT",
            "PUBLIC_NGINX_CUTOVER=NOT_PERFORMED",
            "/auth/login",
            "/dashboard",
        ):
            self.assertIn(marker, script)
        for forbidden in (
            "/usr/local/sbin/vps1-production-deploy",
            "systemctl restart scene-", "systemctl restart vps1",
            "systemctl reload nginx", "nginx -s reload",
            "apt upgrade", "CONCAT_SCENE_SSO_SECRET",
        ):
            self.assertNotIn(forbidden, script)


    def test_web_release_scope_with_real_git_history(self):
        script = ROOT / "deploy/manual-integration-release.sh"
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)

            def git(*args):
                return subprocess.check_output(
                    ["git", "-C", directory, *args], text=True,
                    stderr=subprocess.STDOUT).strip()

            def commit(path, contents):
                target = repo / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(contents)
                git("add", path)
                git("commit", "-m", path)
                return git("rev-parse", "HEAD")

            def verify(approved, tip):
                return subprocess.run(
                    ["bash", "-c", 'source "$1"; verify_web_release "$2" "$3" "$4"',
                     "verify", str(script), directory, approved, tip],
                    capture_output=True, text=True)

            git("init", "-b", "main")
            git("config", "user.name", "Release tests")
            git("config", "user.email", "release@example.invalid")
            commit("concat-web/app.py", "original")
            approved = commit(".github/workflows/concat-web-foundation-ci.yml", "original")
            self.assertEqual(verify(approved, approved).returncode, 0)
            gpu = commit("cloud_render/worker.py", "gpu only")
            exe = commit("upstream/editor.rs", "desktop only")
            for tip in (gpu, exe):
                result = verify(approved, tip)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("CONCAT_WEB_SOURCE_VERIFIED=PASS", result.stdout)

            git("checkout", "-b", "feature")
            feature = commit("unrelated.txt", "feature")
            git("checkout", "main")
            git("merge", "--no-ff", "feature", "-m", "merge feature")
            merged = git("rev-parse", "HEAD")
            self.assertNotEqual(verify(feature, merged).returncode, 0)
            self.assertEqual(verify(approved, merged).returncode, 0)
            self.assertNotEqual(verify("0" * 40, merged).returncode, 0)

            workflow = commit(".github/workflows/concat-web-foundation-ci.yml", "changed")
            result = verify(approved, workflow)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Web workflow changed", result.stderr)
            self.assertEqual(verify(workflow, workflow).returncode, 0)

            changed = commit("concat-web/app.py", "changed")
            result = verify(workflow, changed)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Web source changed", result.stderr)
            self.assertEqual(verify(changed, changed).returncode, 0)


if __name__ == "__main__":
    unittest.main()
