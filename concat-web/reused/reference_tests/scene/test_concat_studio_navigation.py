"""Concat Studio navigation, bookmark and authorization contract tests.

No GPU worker, persistent queues, uploader implementation or production data.
"""
from __future__ import annotations

import ast
import copy
import unittest
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parents[1]
SCENE = ROOT / "apps/scene-builder/part01"
APP = SCENE / "app.py"
SHELL = SCENE / "static/vps1_native_site_shell.js"
BASE = SCENE / "templates/base.html"
CLOUD = SCENE / "cloud_render.py"


def isolated_alias(name, login, admin):
    """Execute only a route handler AST: full app import would start services."""
    tree = ast.parse(APP.read_text(encoding="utf-8"))
    found = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name]
    if len(found) != 1:
        raise AssertionError(f"Expected exactly one {name} route")
    node = copy.deepcopy(found[0])
    node.decorator_list = []
    compiled = compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), str(APP), "exec")
    namespace = {
        "Request": object,
        "require_login": login,
        "require_admin": admin,
        "RedirectResponse": lambda url, status_code: (url, status_code),
    }
    exec(compiled, namespace)
    return namespace[name]


class ConcatStudioNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = Environment(loader=FileSystemLoader(str(SCENE / "templates")), autoescape=True)

    @staticmethod
    def guard_login(request):
        if not request.session.get("authenticated"):
            raise PermissionError("Login required")

    @classmethod
    def guard_admin(cls, request):
        cls.guard_login(request)
        if not request.session.get("is_admin"):
            raise PermissionError("Administrator required")

    def test_landing_and_concat_aliases_preserve_access_gate(self):
        landing = isolated_alias("concat_studio_home", self.guard_login, self.guard_admin)
        concat = isolated_alias("concat_studio_concat", self.guard_login, self.guard_admin)
        guest = SimpleNamespace(session={})
        member = SimpleNamespace(session={"authenticated": True, "is_admin": False})
        admin = SimpleNamespace(session={"authenticated": True, "is_admin": True})
        with self.assertRaises(PermissionError):
            landing(guest)
        self.assertEqual(landing(member), ("/concat-studio/concat/", 303))
        self.assertEqual(landing(admin), ("/concat-studio/concat/", 303))
        with self.assertRaises(PermissionError):
            concat(guest)
        with self.assertRaises(PermissionError):
            concat(member)
        self.assertEqual(concat(admin), ("/cloud-render/", 303))

    def test_legacy_cloud_endpoints_remain_registered(self):
        app = APP.read_text(encoding="utf-8")
        cloud = CLOUD.read_text(encoding="utf-8")
        self.assertIn('request.url.path.rstrip("/") == "/cloud-render"', app)
        self.assertIn('request.url.path.rstrip("/") == "/concat-studio/concat"', app)
        self.assertIn('request.url.path.startswith("/api/cloud-render/")', app)
        self.assertIn('@app.get("/cloud-render/",response_class=HTMLResponse)', cloud)
        self.assertIn('@app.get("/api/cloud-render/media")', cloud)
        self.assertIn('@app.get("/api/cloud-render/config")', cloud)
        self.assertIn('require_admin(req)', cloud)

    def test_shared_navigation_has_exactly_two_concat_children(self):
        shell = SHELL.read_text(encoding="utf-8")
        registry = shell.split("const CONCAT_NAV = Object.freeze([", 1)[1].split("]);", 1)[0]
        self.assertEqual(registry.count('["Concat",'), 1)
        self.assertEqual(registry.count('["Concat Uploader",'), 1)
        self.assertIn('"/concat-studio/concat/"', registry)
        self.assertIn('"/concat-studio/concat-uploader/"', registry)
        experiment = shell.split("const EXPERIMENT_NAV = Object.freeze([", 1)[1].split("]);", 1)[0]
        self.assertNotIn('["Concat",', experiment)
        for label in ("Antidetect Browser", "Upload Studio", "HeyGen Studio", "Voice Clone", "Imagen"):
            self.assertIn('["' + label + '",', experiment)
        self.assertIn('if (concatTabs) concatTabs.hidden = !authenticated', shell)
        self.assertIn('if (concatTabs) concatTabs.hidden = !authorized', shell)

    def test_cloud_render_template_gets_concat_rail_only(self):
        for admin in (True, False):
            with self.subTest(admin=admin):
                rendered = self.env.get_template("base.html").render(
                    page="cloud_render",
                    request=SimpleNamespace(
                        session={"username": "member", "is_admin": admin, "studio_access": ()},
                        query_params={},
                    ),
                    username="member",
                )
                self.assertIn('href="/concat-studio/">Concat Studio</a>', rendered)
                self.assertEqual(rendered.count('aria-label="Concat Studio"'), 1)
                self.assertEqual(rendered.count('aria-label="Experiment Studio"'), 0)
                subnav = rendered.split('aria-label="Concat Studio"', 1)[1].split("</nav>", 1)[0]
                self.assertEqual(subnav.count("<a "), 2)
                self.assertIn('href="/concat-studio/concat/"', subnav)
                self.assertIn('href="/concat-studio/concat-uploader/"', subnav)

    def test_experiment_studio_keeps_unrelated_links(self):
        rendered = self.env.get_template("base.html").render(
            page="experiment_studio",
            request=SimpleNamespace(
                session={"username": "owner", "is_admin": True, "studio_access": ()},
                query_params={},
            ),
            username="owner",
        )
        experiment = rendered.split('aria-label="Experiment Studio"', 1)[1].split("</nav>", 1)[0]
        self.assertNotIn(">Concat</a>", experiment)
        for label in ("Antidetect Browser", "Upload Studio", "HeyGen Studio", "Voice Clone", "Imagen"):
            self.assertIn(">" + label + "</a>", experiment)
        self.assertEqual(rendered.count('aria-label="Concat Studio"'), 0)


if __name__ == "__main__":
    unittest.main()
