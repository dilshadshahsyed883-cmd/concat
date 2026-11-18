"""Dashboard contract tests; included by existing test_reused_*.py CI discovery."""
import copy
import unittest
from fastapi.testclient import TestClient

from app.auth import Principal
from app.main import create_app

NOW = "2026-11-18T12:00:00Z"
EARLIER = "2026-11-17T12:00:00Z"
SHA = "a" * 64


def scene_data(owner):
    own_rows = owner.casefold() == "alice"
    return {
        "owner_username": owner,
        "totals": {"total": 2 if own_rows else 0, "completed": 1 if own_rows else 0,
                   "queued": 1 if own_rows else 0, "active": 0, "failed": 0},
        "queue": {"state": "stopped", "queued": 1 if own_rows else 0, "active": 0},
        "jobs": ([{"id": "render-a", "owner_username": owner, "name": "Scene One",
                   "status": "Completed", "updated_at": EARLIER}] if own_rows else []),
        "videos": {"total": 1 if own_rows else 0, "items": ([
            {"id": "media-a", "owner_username": owner, "name": "Scene One.mp4",
             "state": "ready", "sha256": SHA, "updated_at": NOW}] if own_rows else [])},
        "gpu": {"available": False if own_rows else None,
                "status": "unreachable" if own_rows else "unknown", "checked_at": NOW if own_rows else None},
    }


def upload_data(owner):
    own_rows = owner.casefold() == "alice"
    return {
        "owner_username": owner,
        "totals": {"total": 3 if own_rows else 0, "completed": 2 if own_rows else 0,
                   "queued": 1 if own_rows else 0, "active": 0, "failed": 0},
        "queue": {"state": "paused", "queued": 1 if own_rows else 0, "active": 0},
        "jobs": ([{"id": "upload-a", "owner_username": owner, "name": "Publish Scene One",
                   "status": "queued", "updated_at": NOW}] if own_rows else []),
    }


class Producer:
    def __init__(self, factory):
        self.factory = factory
        self.calls = []
        self.mutate = None

    def dashboard_snapshot(self, *, owner_username, limit):
        self.calls.append((owner_username, limit))
        result = copy.deepcopy(self.factory(owner_username))
        if self.mutate:
            self.mutate(result)
        return result


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.principal = Principal("alice", is_active=True, concat_granted=True)
        self.app.state.identity_resolver = lambda request: self.principal
        self.render_source = Producer(scene_data)
        self.upload_source = Producer(upload_data)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()

    def connect(self):
        self.app.state.concat_dashboard_provider = self.render_source
        self.app.state.uploader_dashboard_provider = self.upload_source

    def test_auth_is_verified_and_never_header_identity(self):
        self.app.state.identity_resolver = None
        for path in ("/dashboard", "/api/v1/dashboard/overview"):
            result = self.client.get(path, headers={"X-Auth-User": "admin", "X-Is-Admin": "true"})
            self.assertEqual(result.status_code, 401)
        self.app.state.identity_resolver = lambda _: Principal("alice", is_active=False, concat_granted=True)
        self.assertEqual(self.client.get("/api/v1/dashboard/overview").status_code, 401)
        self.app.state.identity_resolver = lambda _: Principal("alice", is_active=True, concat_granted=False)
        self.assertEqual(self.client.get("/api/v1/dashboard/overview").status_code, 403)

    def test_no_fake_counters_if_services_not_ready(self):
        result = self.client.get("/api/v1/dashboard/overview")
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.headers["cache-control"], "private, no-store")
        page = self.client.get("/dashboard")
        self.assertEqual(page.status_code, 200)
        self.assertIn("data-stat=\"renders.total\">—", page.text)
        self.assertIn("data-stat=\"videos.total\">—", page.text)
        self.assertIn("dashboard/assets/dashboard.js", page.text)
        self.assertEqual(page.text.count('class="nav-group"'), 3)
        self.assertIn('id="nav-toggle"', page.text)
        self.assertIn('name="viewport"', page.text)

    def test_real_owner_snapshot_composition_and_order(self):
        self.connect()
        result = self.client.get("/api/v1/dashboard/overview?limit=8", headers={"X-Auth-User": "bob"})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.headers["cache-control"], "private, no-store")
        data = result.json()
        self.assertEqual(data["renders"]["total"], 2)
        self.assertEqual(data["uploads"]["completed"], 2)
        self.assertEqual(data["videos"]["total"], 1)
        self.assertEqual(data["renders"]["queue"]["queued"], 1)
        self.assertEqual(data["uploads"]["queue"]["state"], "paused")
        self.assertIs(data["gpu"]["available"], False)
        self.assertEqual(data["recent_activity"][0]["kind"], "upload")
        self.assertEqual(data["recent_activity"][-1]["kind"], "render")
        self.assertEqual(self.render_source.calls, [("alice", 8)])
        self.assertEqual(self.upload_source.calls, [("alice", 8)])
        # No media checksum or owner-internal fields exposed as UI row data.
        self.assertNotIn(SHA, result.text)
        self.assertNotIn("owner_username", data["renders"]["jobs"][0])

    def test_admin_cannot_see_other_owner_and_query_owner_ignored(self):
        self.connect()
        self.principal = Principal("bob", is_active=True, concat_granted=True, is_admin=True)
        response = self.client.get("/api/v1/dashboard/overview?owner_username=alice")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["renders"]["total"], 0)
        self.assertEqual(data["uploads"]["total"], 0)
        self.assertEqual(data["videos"]["items"], [])
        self.assertEqual(self.render_source.calls[-1][0], "bob")
        self.assertEqual(self.upload_source.calls[-1][0], "bob")

    def test_cross_owner_row_or_snapshot_fails_closed(self):
        self.connect()
        self.render_source.mutate = lambda d: d["jobs"][0].update(owner_username="bob")
        self.assertEqual(self.client.get("/api/v1/dashboard/overview").status_code, 502)
        self.render_source.mutate = lambda d: d.update(owner_username="bob")
        self.assertEqual(self.client.get("/api/v1/dashboard/overview").status_code, 502)

    def test_unverified_media_and_invalid_counts_fail_closed(self):
        self.connect()
        self.render_source.mutate = lambda d: d["videos"]["items"][0].update(sha256="bad")
        self.assertEqual(self.client.get("/api/v1/dashboard/overview").status_code, 502)
        self.render_source.mutate = lambda d: d["totals"].update(total=True)
        self.assertEqual(self.client.get("/api/v1/dashboard/overview").status_code, 502)
        self.render_source.mutate = lambda d: d["totals"].update(completed=-1)
        self.assertEqual(self.client.get("/api/v1/dashboard/overview").status_code, 502)

    def test_service_error_is_unavailable_not_zero(self):
        self.connect()
        class Broken:
            def dashboard_snapshot(self, *, owner_username, limit):
                raise RuntimeError("remote database failed")
        self.app.state.concat_dashboard_provider = Broken()
        result = self.client.get("/api/v1/dashboard/overview")
        self.assertEqual(result.status_code, 503)
        self.assertNotIn("remote database failed", result.text)

    def test_limits_assets_and_mobile_layout(self):
        self.connect()
        self.assertEqual(self.client.get("/api/v1/dashboard/overview?limit=26").status_code, 422)
        self.assertEqual(self.client.get("/api/v1/dashboard/overview?limit=0").status_code, 422)
        self.assertEqual(self.client.get("/api/v1/dashboard/overview?limit=1").status_code, 200)
        self.assertEqual(self.render_source.calls[-1], ("alice", 1))
        css = self.client.get("/dashboard/assets/dashboard.css")
        self.assertEqual(css.status_code, 200)
        self.assertIn("@media(max-width:480px)", css.text)
        js = self.client.get("/dashboard/assets/dashboard.js")
        self.assertEqual(js.status_code, 200)
        self.assertIn("credentials: \"same-origin\"", js.text)
        self.assertIn("textContent", js.text)

if __name__ == "__main__":
    unittest.main()
