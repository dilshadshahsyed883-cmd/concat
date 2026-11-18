import unittest
from fastapi.testclient import TestClient
from app.main import create_app
from app.auth import Principal
from app.contracts.navigation import PAGES

class FoundationTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()

    def test_public_health_and_no_untrusted_identity(self):
        self.assertEqual(self.client.get("/healthz").json(), {"ok": True, "service": "concat-web"})
        for path in ("/", "/dashboard", "/concat/production", "/concat/media", "/concat/gpu-config",
                     "/concat-uploader/sheets-sync", "/concat-uploader/channels",
                     "/concat-uploader/upload-engine", "/concat-uploader/queue", "/admin", "/api/v1/session"):
            with self.subTest(path=path):
                reply = self.client.get(path, headers={"X-Auth-User": "admin", "X-Is-Admin": "1"},
                                        follow_redirects=False)
                self.assertEqual(reply.status_code, 401)

    def test_injected_resolver_wires_authenticated_routes(self):
        self.app.state.identity_resolver = lambda _: Principal("alice", is_active=True, concat_granted=True)
        for page in PAGES:
            with self.subTest(page=page.key):
                res = self.client.get(page.path)
                self.assertEqual(res.status_code, 403 if page.key == "admin" else 200)
                if page.key != "admin":
                    self.assertIn(page.title, res.text)
        data = self.client.get("/api/v1/session").json()
        self.assertEqual(data["username"], "alice")
        self.assertFalse(data["is_admin"])

    def test_admin_area_requires_admin_but_admin_same_owner(self):
        self.app.state.identity_resolver = lambda _: Principal("admin-one", is_active=True, is_admin=True, concat_granted=True)
        self.assertEqual(self.client.get("/admin").status_code, 200)
        self.assertEqual(self.client.get("/api/v1/session").json()["username"], "admin-one")

    def test_revoked_and_invalid_accounts_rejected(self):
        for principal, expected in (
            (Principal("alice", is_active=False, concat_granted=True), 401),
            (Principal("alice", is_active=True, concat_granted=False), 403),
            (Principal("../../bad", is_active=True, concat_granted=True), 401),
            (None, 401),
        ):
            self.app.state.identity_resolver = lambda _, p=principal: p
            self.assertEqual(self.client.get("/dashboard").status_code, expected)

    def test_exact_top_level_navigation_and_mobile_shell(self):
        self.app.state.identity_resolver = lambda _: Principal("alice", is_active=True, concat_granted=True)
        page = self.client.get("/dashboard").text
        self.assertEqual(page.count('class="nav-group"'), 3)
        for name in ("Dashboard", "Concat Studio", "Admin", "Sheets Sync", "GPU Config", "Upload Engine"):
            self.assertIn(name, page)
        self.assertIn('name="viewport"', page)
        self.assertIn('id="nav-toggle"', page)

    def test_unknown_path_not_feature_stub(self):
        self.assertEqual(self.client.get("/api/v1/concat/start").status_code, 404)

if __name__ == "__main__":
    unittest.main()
