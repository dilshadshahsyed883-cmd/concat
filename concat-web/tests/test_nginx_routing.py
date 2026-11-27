"""Offline Nginx security and fail-closed terminal cutover contracts.

Never calls the VPS, touches /etc/nginx or reloads production in CI.
"""
from pathlib import Path
import re
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "deploy/nginx-site.example.conf"
CUTOVER = ROOT / "deploy/nginx-cutover.sh"


class ConcatNginxRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conf = TEMPLATE.read_text()
        cls.script = CUTOVER.read_text()

    def test_separate_server_name_ssl_and_loopback(self):
        self.assertEqual(self.conf.count("server_name concat.scenebuilder.online;"), 2)
        self.assertEqual(self.conf.count("ssl_certificate __CONCAT_TLS_FULLCHAIN__;"), 1)
        self.assertEqual(self.conf.count("ssl_certificate_key __CONCAT_TLS_PRIVKEY__;"), 1)
        self.assertEqual(self.conf.count("__CONCAT_TLS_FULLCHAIN__"), 1)
        self.assertEqual(self.conf.count("__CONCAT_TLS_PRIVKEY__"), 1)
        self.assertIn("proxy_pass http://127.0.0.1:18873;", self.conf)
        self.assertIn("return 308 https://concat.scenebuilder.online$request_uri;", self.conf)
        self.assertIn("Strict-Transport-Security", self.conf)
        self.assertIn("access_log off;", self.conf)
        self.assertNotIn("listen 18873", self.conf)
        self.assertNotIn("app.scenebuilder.online", self.conf)
        self.assertNotIn("CONCAT_SCENE_SSO_SECRET", self.conf)
        self.assertNotIn("concat-sso/authorize", self.conf)

    def test_no_spoofed_identity_headers(self):
        self.assertIn("proxy_set_header Host $host;", self.conf)
        self.assertIn("proxy_set_header X-Forwarded-Proto https;", self.conf)
        self.assertIn("proxy_set_header X-Forwarded-For $remote_addr;", self.conf)
        for header in ("Authorization", "Forwarded", "X-Auth-User",
                       "X-Is-Admin", "X-App-Access", "X-Forwarded-User",
                       "X-Authenticated-User", "X-Scene-User",
                       "X-Remote-User", "X-User", "X-Admin"):
            with self.subTest(header=header):
                self.assertRegex(self.conf,
                                 r"proxy_set_header\s+" + re.escape(header) +
                                 r'\s+"";')
        self.assertNotIn('proxy_set_header X-Cloud-Render-Token "";', self.conf)

    def test_guarded_script_is_syntax_valid(self):
        result = subprocess.run(["bash", "-n", str(CUTOVER)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('MODE="${1:-audit}"', self.script)
        self.assertIn("CHANGES=NONE", self.script)
        self.assertIn("nginx_valid || fail", self.script)
        self.assertIn("CONCAT_RELEASE_SHA_MISMATCH", self.script)
        self.assertIn("CONCAT_ANONYMOUS_DENIAL_FAILED", self.script)
        self.assertIn("CONCAT_HTTPS_LOGIN_UNAVAILABLE", self.script)
        self.assertIn("CONCAT_NGINX_ROLLBACK=STARTED", self.script)
        self.assertIn("NGINX_CONFIG_DRIFT_REVIEW_REQUIRED", self.script)
        self.assertIn("EXISTING_SITE_HTTP_CHANGED", self.script)
        self.assertIn("openssl x509", self.script)
        self.assertIn("--resolve", self.script)

    def test_mutations_are_concat_nginx_only(self):
        for required in ("/etc/nginx/sites-available/concat-web.conf",
                         "/etc/nginx/sites-enabled/concat-web.conf",
                         "/etc/concat-web/nginx-site-managed",
                         "systemctl reload nginx"):
            self.assertIn(required, self.script)
        for forbidden in ("systemctl restart nginx", "systemctl restart vps1",
                          "systemctl restart scene-", "systemctl restart concat-web",
                          "systemctl reload vps1", "sudo -n bash -s",
                          "certbot --nginx", "apt upgrade", "CONCAT_SCENE_SSO_SECRET",
                          "rm -rf /etc/nginx", "chmod -R"):
            self.assertNotIn(forbidden, self.script)

    def test_cutover_is_never_part_of_ci_automatic_deploy(self):
        workflow = (ROOT.parent / ".github/workflows/concat-web-deploy.yml").read_text()
        self.assertNotIn("nginx-cutover.sh activate", workflow)
        self.assertNotIn("ssh ", workflow)
        self.assertNotIn("scp ", workflow)
        self.assertNotIn("push:", workflow)


if __name__ == "__main__":
    unittest.main()
