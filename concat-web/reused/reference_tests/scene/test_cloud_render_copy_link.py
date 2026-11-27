"""Media Copy Link: authenticated VPS-only handoff; existing Drive flow is untouched."""
import hashlib
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

MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 81


class CloudRenderCopyLinkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        app = FastAPI()

        def login(request: Request):
            if not request.session.get("authenticated"):
                raise HTTPException(401, "Login required")

        def admin(request: Request):
            if not request.session.get("is_admin"):
                raise HTTPException(403, "Administrator access required")

        self.manager = register_cloud_render(
            app, Jinja2Templates(directory=str(SCENE / "templates")),
            self.temp.name, admin, login,
        )

        @app.get("/_test/session/{role}")
        def session(request: Request, role: str):
            request.session.clear()
            if role in ("admin", "member"):
                request.session.update({"authenticated": True, "is_admin": role == "admin"})
            return {"role": role}

        app.add_middleware(SessionMiddleware, secret_key="copy-link-fixture")
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.addCleanup(self.manager.pool.shutdown, wait=True)

    def test_copy_link_uses_existing_authenticated_mp4_endpoint(self):
        self.manager.receive(
            "media-123", 0, MP4, name="Sample.mp4", size=len(MP4),
            sha=hashlib.sha256(MP4).hexdigest(),
        )
        path = "/api/cloud-render/media/media-123/file"
        self.client.get("/_test/session/guest")
        self.assertIn(self.client.get(path).status_code, (401, 403))
        self.client.get("/_test/session/member")
        self.assertIn(self.client.get(path).status_code, (401, 403))
        self.client.get("/_test/session/admin")
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, MP4)
        self.assertEqual(response.headers["content-type"], "video/mp4")
        self.assertIn("no-store", response.headers["cache-control"])
        self.assertEqual(self.client.get(path + "?download=1").status_code, 200)
        self.assertEqual(self.client.get("/api/cloud-render/media/../file").status_code, 404)

    def test_copied_vps_link_unavailable_after_drive_delivery(self):
        self.manager.receive(
            "media-456", 0, MP4, name="Delivered.mp4", size=len(MP4),
            sha=hashlib.sha256(MP4).hexdigest(),
        )
        with self.manager.connection() as con:
            con.execute(
                "UPDATE files SET state='delivered', drive_path=? WHERE id=?",
                ("gdrive:Cloud Render/Delivered.mp4", "media-456"),
            )
        self.manager.local("media-456").unlink()
        self.client.get("/_test/session/admin")
        self.assertEqual(
            self.client.get("/api/cloud-render/media/media-456/file").status_code, 404
        )
        self.assertEqual(self.manager.listing()["items"][0]["state"], "delivered")

    def test_button_only_for_states_supported_by_file_route(self):
        js = (SCENE / "static/cloud_render.js").read_text(encoding="utf-8")
        html = (SCENE / "templates/cloud_render.html").read_text(encoding="utf-8")
        self.assertIn('button("Copy Link",()=>copyMediaLink(file.id))', js)
        self.assertIn('if(["ready","failed","queued"].includes(file.state))', js)
        self.assertIn('new URL("/api/cloud-render/media/" + encodeURIComponent(id) + "/file", window.location.origin)', js)
        self.assertIn("await navigator.clipboard.writeText(url)", js)
        self.assertIn("Copy Link for Concat Uploader", html)
        # Existing Drive preference/action/API are not displaced by Copy Link.
        self.assertIn('id="cr-auto"', html)
        self.assertIn('id="cr-save-auto"', html)
        self.assertIn('"Upload to Drive"', js)
        self.assertIn('"/media/"+encodeURIComponent(file.id)+"/drive"', js)
        self.assertIn('"/config",{auto_upload:', js)


if __name__ == "__main__":
    unittest.main()
