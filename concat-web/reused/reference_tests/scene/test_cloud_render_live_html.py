"""Real HTML template and admin-role route acceptance for Cloud Render."""
from __future__ import annotations
import re
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

ROOT = Path(__file__).resolve().parents[1]
SCENE = ROOT / "apps/scene-builder/part01"
sys.path.insert(0, str(SCENE))
from cloud_render import register_cloud_render

class CloudRenderHtmlTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        app = FastAPI()
        def login(req: Request):
            if not req.session.get("authenticated"):
                raise HTTPException(401, "Login required")
        def admin(req: Request):
            if not req.session.get("is_admin"):
                raise HTTPException(403, "Administrator access required")
        self.manager = register_cloud_render(app, Jinja2Templates(directory=str(SCENE/"templates")),
                                             self.tmp.name, admin, login)
        @app.get("/_fixture/session/{role}")
        def session(req: Request, role: str):
            req.session.clear()
            if role in ("admin", "member"):
                req.session.update({"authenticated": True, "is_admin": role=="admin",
                                    "username": "owner" if role=="admin" else "member"})
            return {"role": role}
        app.add_middleware(SessionMiddleware, secret_key="test-cloud-render-session")
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.addCleanup(self.manager.pool.shutdown, wait=True)

    def test_admin_real_html_and_gpu_config_routes(self):
        self.client.get("/_fixture/session/admin")
        res = self.client.get("/cloud-render/")
        self.assertEqual(res.status_code,200)
        self.assertIn("text/html",res.headers.get("content-type",""))
        for marker in ("Concat", "GPU Config", "Media", 'data-tab="gpu"', 'data-tab="media"'):
            self.assertIn(marker,res.text)
        self.assertIn('href="/concat-studio/concat/"',res.text)
        tabs = re.findall(r'data-tab="([^"]+)" aria-selected="(true|false)"', res.text)
        self.assertEqual(tabs, [("production", "true"), ("media", "false"), ("gpu", "false")])
        self.assertIn('id="cr-media" hidden', res.text)
        self.assertIn('id="cr-gpu" hidden', res.text)
        self.assertEqual(res.text.count('data-tab="gpu"'),1)
        self.assertEqual(res.text.count('data-tab="media"'),1)
        # Cloud Render owns its page heading and inner tabs; not Video Studio's shell.
        self.assertNotIn('<nav data-video-studio-subnav="1"', res.text)
        self.assertNotIn('<h1>Concat</h1>', res.text)
        self.assertEqual(res.text.count('id="cloud-heading"'), 1)
        self.assertIn('href="/concat-studio/concat/">Concat</a>', res.text)
        self.assertNotIn('class="active" aria-current="page" href="/scene-builder/">Video Studio</a>', res.text)
        cfg = self.client.get("/api/cloud-render/config")
        self.assertEqual(cfg.status_code,200)
        self.assertFalse(cfg.json()["image_ready"])
        self.assertEqual(self.client.get("/api/cloud-render/media").json()["items"],[])

    def test_member_and_guest_cannot_read_media(self):
        for role in ("member","guest"):
            self.client.get("/_fixture/session/"+role)
            res = self.client.get("/cloud-render/")
            self.assertIn(res.status_code,(401,403))
            self.assertIn(self.client.get("/api/cloud-render/config").status_code,(401,403))
            self.assertIn(self.client.get("/api/cloud-render/media").status_code,(401,403))
            self.assertIn(self.client.get("/api/cloud-render/production").status_code,(401,403))
            self.assertIn(self.client.post("/api/cloud-render/production/queue/start").status_code,(401,403))

    def test_mutation_requires_admin_csrf(self):
        self.client.get("/_fixture/session/admin")
        self.assertEqual(self.client.post("/api/cloud-render/config",json={"auto_upload":True}).status_code,403)
        self.assertFalse(self.client.get("/api/cloud-render/config").json()["auto_upload"])

    def test_production_admin_can_scan_control_and_cancel_with_csrf(self):
        self.client.get("/_fixture/session/admin")
        page = self.client.get("/cloud-render/")
        token = re.search(r'data-csrf="([^"]+)"', page.text).group(1)
        headers = {"X-Cloud-Render-CSRF": token}
        path = "/api/cloud-render/production"
        self.assertEqual(self.client.post(path+"/queue/start").status_code, 403)
        self.assertEqual(self.client.post(path+"/scan", headers=headers, json={"link":"https://elsewhere.com"}).status_code, 422)
        result = self.client.post(path+"/scan", headers=headers, json={
            "link":"https://drive.google.com/file/d/linked-project/view?resourcekey=shared-key"
        })
        self.assertEqual(result.json(), {"added": 1, "duplicates": 0})
        data = self.client.get(path).json()
        self.assertEqual(data["state"], "stopped")
        job = data["items"][0]
        self.assertEqual(job["state"], "Queued")
        self.assertNotIn("shared-key", str(data))
        self.assertEqual(self.client.post(f"{path}/jobs/{job['id']}/cancel", headers=headers).status_code, 200)
        self.assertEqual(self.client.get(path).json()["items"][0]["state"], "Cancelled")
        self.assertEqual(self.client.post(path+"/queue/pause", headers=headers).json()["state"], "paused")


if __name__ == "__main__":
    unittest.main()
