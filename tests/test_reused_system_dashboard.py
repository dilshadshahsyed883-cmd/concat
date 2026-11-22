"""Reuse VPS1 monitor: secured, read-only endpoint and exact panel scope."""
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.auth import Principal
from app.main import create_app


SNAPSHOT = {
    "ok": True, "generated_at_local": "11:45:00",
    "vps": {
        "cpu_percent": 11.5, "cpu_cores": 4,
        "ram": {"percent": 47.9, "used_gb": 3.6, "total_gb": 7.6},
        "disk": {"percent": 39.4, "used_gb": 28.2, "total_gb": 71.6, "free_gb": 43.4},
        "load_1m": 0.53, "uptime_human": "33d 14h 51m",
    },
    "services": [{"name": "unit" + str(i), "active": True} for i in range(9)],
}


class SystemDashboardTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()

    def test_auth_does_not_trust_headers_or_revoked_access(self):
        for path in ("/api/v1/dashboard/system", "/dashboard"):
            self.assertEqual(self.client.get(path, headers={"X-Auth-User": "admin"}).status_code, 401)
        self.app.state.identity_resolver = lambda _: Principal("alice", is_active=True, concat_granted=False)
        self.assertEqual(self.client.get("/api/v1/dashboard/system").status_code, 403)

    def test_endpoint_uses_real_producer_and_no_cache(self):
        self.app.state.identity_resolver = lambda _: Principal("alice", is_active=True, concat_granted=True)
        with patch("app.modules.dashboard.routes.system_dashboard_snapshot", return_value=SNAPSHOT) as source:
            r = self.client.get("/api/v1/dashboard/system")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.headers["cache-control"], "private, no-store")
        self.assertEqual(r.json()["vps"]["disk"]["free_gb"], 43.4)
        self.assertEqual(len(r.json()["services"]), 9)
        self.assertNotIn("owner_username", r.text)
        self.assertNotIn("uploads", r.json())
        self.assertEqual(source.call_count, 1)

    def test_unavailable_snapshot_is_not_reported_as_zero(self):
        self.app.state.identity_resolver = lambda _: Principal("alice", is_active=True, concat_granted=True)
        with patch("app.modules.dashboard.routes.system_dashboard_snapshot", side_effect=OSError("monitor failed")):
            r = self.client.get("/api/v1/dashboard/system")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.headers["cache-control"], "private, no-store")
        self.assertNotIn("monitor failed", r.text)

    def test_dashboard_contains_only_vps1_status_resources(self):
        self.app.state.identity_resolver = lambda _: Principal("alice", is_active=True, concat_granted=True)
        page = self.client.get("/dashboard")
        self.assertEqual(page.status_code, 200)
        html = page.text
        self.assertEqual(html.count('class="nav-group"'), 3)
        for label in ("VPS Status", "CPU Usage", "RAM Usage", "Storage Usage",
                      "Services Health", "System Resources", "Auto Refresh"):
            self.assertIn(label, html)
        for obsolete in ("Render queue", "Upload queue", "Recent completed videos",
                         "Recent activity", "Quick navigation"):
            self.assertNotIn(obsolete, html)
        self.assertIn('id="dashboard-refresh-rate"', html)
        self.assertIn("dashboard.js?v=vps1-monitor-20261119", html)
        js = self.client.get("/dashboard/assets/dashboard.js").text
        self.assertIn("/api/v1/dashboard/system", js)
        self.assertNotIn("'/api/dashboard/v1'", js)


if __name__ == "__main__":
    unittest.main()
