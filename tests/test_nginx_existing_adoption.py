"""Offline tests for hash-guarded existing Concat Nginx vhost adoption.

Never run Nginx, mutate /etc/nginx, or contact VPS from CI.
"""
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy/nginx-adopt-existing.py"
SPEC = importlib.util.spec_from_file_location("concat_nginx_existing_adoption", SCRIPT)
adopt = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(adopt)

STATIC = """server {
    listen 80;
    server_name concat.scenebuilder.online;
    location ^~ /.well-known/acme-challenge/ {
        root /var/www/letsencrypt;
        try_files $uri =404;
    }
    location / {
        root /var/www/concat-placeholder;
        return 301 https://concat.scenebuilder.online$request_uri;
    }
}
server {
    listen 443 ssl;
    server_name concat.scenebuilder.online;
    ssl_certificate /etc/letsencrypt/live/concat.scenebuilder.online/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/concat.scenebuilder.online/privkey.pem;
    location / {
        root /var/www/concat-placeholder;
        try_files $uri $uri/ /index.html;
    }
}
"""


class ExistingNginxSiteAdoptionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SCRIPT.read_text()
        cls.proxy = (ROOT / "deploy/nginx-proxy-location.conf").read_text()

    def test_exactly_https_root_replaced_acme_and_redirect_preserved(self):
        rendered = adopt.transform_existing_site(STATIC, self.proxy)
        original_http = STATIC[:STATIC.index("server {", STATIC.index("server {") + 1)]
        self.assertTrue(rendered.startswith(original_http))
        self.assertIn("location ^~ /.well-known/acme-challenge/", rendered)
        self.assertIn("root /var/www/letsencrypt;", rendered)
        self.assertIn("return 301 https://concat.scenebuilder.online$request_uri;", rendered)
        self.assertIn("ssl_certificate /etc/letsencrypt/live/concat.scenebuilder.online/fullchain.pem;", rendered)
        self.assertIn("ssl_certificate_key /etc/letsencrypt/live/concat.scenebuilder.online/privkey.pem;", rendered)
        self.assertNotIn("try_files $uri $uri/ /index.html;", rendered)
        self.assertEqual(rendered.count("proxy_pass http://127.0.0.1:18873;"), 1)
        self.assertEqual(rendered.count("server_name concat.scenebuilder.online;"), 2)

    def test_reject_foreign_name_or_unknown_shared_server(self):
        with self.assertRaisesRegex(RuntimeError, "OTHER_SERVER_NAME"):
            adopt.transform_existing_site(
                STATIC.replace("server_name concat.scenebuilder.online;",
                               "server_name app.scenebuilder.online;", 1), self.proxy)

    def test_reject_externally_patched_proxy(self):
        with self.assertRaisesRegex(RuntimeError, "UNEXPECTED_EXISTING_PROXY"):
            adopt.transform_existing_site(
                STATIC.replace("try_files $uri $uri/ /index.html;",
                               "proxy_pass http://127.0.0.1:9000;"), self.proxy)

    def test_reject_extra_root_or_nested_location(self):
        with self.assertRaisesRegex(RuntimeError, "EXPECTED_ONE_HTTPS_ROOT"):
            adopt.transform_existing_site(
                STATIC.replace("    location / {\n        root /var/www/concat-placeholder;\n        try_files $uri $uri/ /index.html;\n    }",
                               "    location / {\n        try_files $uri =404;\n    }\n"
                               "    location / {\n        try_files $uri =404;\n    }"), self.proxy)
        with self.assertRaisesRegex(RuntimeError, "NESTED_ROOT"):
            adopt.transform_existing_site(
                STATIC.replace("try_files $uri $uri/ /index.html;",
                               "if ($uri = /) { return 200; }\n        try_files $uri $uri/ /index.html;"),
                self.proxy)

    def test_restrict_to_audited_site_with_secure_backup_and_checks(self):
        script = self.source
        self.assertIn("d0371b9ec8296a2e57dafafc2b7dda8268d70d23c0b657f5f46ea35e2c1819e5", script)
        for label in ("AUDITED_SITE_FINGERPRINT_DRIFT", "BACKUP_ALREADY_EXISTS",
                      "CONCAT_STAGE_SHA_MISMATCH", "CONCAT_LOOPBACK_APP_SECURITY_NOT_READY",
                      "CONCAT_PUBLIC_HTTPS_IDENTITY_OR_AUTH_FAILED",
                      "CONCAT_HTTPS_HEALTH_OR_AUTH_FAILED", "SCENE_HTTPS_STATUS_CHANGED",
                      "CURRENT_SITE_EDITED_OUT_OF_BAND", "BACKUP_CHECKSUM_FAILURE",
                      "atomic_write(AVAILABLE, old, 0o644)", "reload_nginx()",
                      "CONCAT_NGINX_ROLLBACK=PASS"):
            with self.subTest(label=label):
                # One static contract may intentionally be named differently
                # between initial-new-site cutover and existing-site adoption.
                self.assertIn(label, script)
        for forbidden in ("systemctl restart scene-", "systemctl restart nginx",
                          "systemctl restart vps1", "apt upgrade", "certbot --nginx",
                          "rm -rf /etc/nginx"):
            self.assertNotIn(forbidden, script)

    def test_manual_release_bundles_nginx_adoption_code(self):
        script = (ROOT / "deploy/manual-integration-release.sh").read_text()
        self.assertIn("app reused deploy run.py pyproject.toml", script)


if __name__ == "__main__":
    unittest.main()
