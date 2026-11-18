# COPY → ADAPT → VERIFY: first-party reuse inventory

**Copy baseline:** 18 November 2026. Source repository: [dilshadshahsyed883-cmd/vps1-deploy-control](https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control).

The original author/account owns both repositories. Files copied here are **first-party implementation source**, not third-party vendored packages. No private runtime data, credential material, OAuth JSON, environment secrets, model weights, binary executables or database dumps were copied. Existing third-party packages are declared by upstream `requirements.txt` and resolved at installation; no third-party license or downloaded package source is redistributed by this copy. Verify transitive dependency licensing and security before production release.

| Area | Copied implementation | Source provenance | Notes |
|---|---|---|---|
| Cloud Render | `reused/scene/part01/cloud_render.py`, `cloud_render_production.py` | VPS_1 `main`, [original paths](https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/tree/main/apps/scene-builder/part01) | Contains working rendering, ZIP validation, local media and canonical Drive Bridge behavior; **admin-only and ownerless upstream**, not safe to activate until Agent 5 |
| Cloud Render UI | `reused/scene/part01/templates/cloud_render.html`, `static/cloud_render.js`, `cloud_render.css` | VPS_1 `feature/concat-studio-navigation-20261118`, [PR #769](https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/pull/769) | Preserves existing Production/Media/GPU config UI and Copy Video Link contract |
| Existing nav reference | `reused/scene/part01/templates/base.html`, `static/vps1_native_site_shell.js/css` | [PR #769](https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/pull/769) | Source for responsive patterns; original sidebar also contains unrelated VPS_1 apps, so **do not mount as-is** |
| Uploader engine | `reused/uploader/part01/lum_engine/*.py` | VPS_1 `feature/concat-uploader-reuse-20261118`, [PR #770](https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/pull/770) | Complete original engine including Concat source, queue, Sheets, OAuth, auth and worker modules. Engine imports include legacy disabled auto-engine dependencies. **Do not run until trusted identity + media ownership verified.** |
| Uploader core | `reused/uploader/part01/desktop_core/modules/**` | [PR #770](https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/pull/770) | Original SQLite queue, uploads, YouTube stages and OAuth/token helpers. Explicitly preserve directory layout to retain imports |
| Uploader UI | `reused/uploader/part01/templates/**`, `static/**`, `run.py`, `requirements.txt` | [PR #770](https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/pull/770) | Four existing tabs, channel subtabs, controls and dependencies. Runtime needs its own independent data root |
| Security audit | `docs/REFERENCE_AGENT4_SECURITY_AUDIT.md` | [PR #768](https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/pull/768) | Source audit reference only; no owner-isolation implementation in that PR |
| Reference test corpus | `reused/reference_tests/{scene,uploader,security}` | Same corresponding branches/PRs | Original tests retained for agents to adapt without reconstructing assertions. Paths refer to original repo and therefore are **not** run directly |
| Portable offline tests | `tests/test_reused_*.py` | PR #770 tests adapted **only** source root `apps/copyright-uploader/part01` → `reused/uploader/part01` | Reuse verification, no network/YouTube publication |

## Remaining hard integration boundaries

The originals are copied *as working code* and are not silently replaced with mock facades. **They are not yet connected to the independent protected site.** This is intentional: Agent 1 owns only the secure foundation, while Agents 2–5 adapt the copied modules into their dedicated routes and runtime.

- **Cloud owner storage**: copied Cloud Render still has globally stored ownerless `files` and `production_jobs`. Agent 5 and Agent 2 must implement verified owner attribution, per-owner scoping, callback owner binding and quarantined legacy data before removing admin restrictions.
- **Uploader trust**: copied uploader's `X-Auth-User` handler is designed for a *trusted loopback Nginx auth_request*, not public clients. No reverse proxy or port is enabled. Until verified Scene SSO exists it must not be mounted or exposed. Agent 3 retains Concat-only `OWNER_SCOPED` behavior.
- **Drive Bridge**: preserve `scene_rclone_env`, canonical remote identity, checksum and remote stat verification, known owner-linked source copy, receipt and cleanup sequencing. Do not replace with arbitrary HTTP download or copy tokens.
- **Contracts**: public `app/main.py` includes only shells that are 401 by default; see `docs/AGENT_HANDOFF.md` for frozen module paths. No API mutation or producer/worker action is accessible.
- **Packaging**: the foundation wheel contains the `app` package; source reuse is retained alongside it as a separate checkout tree for Agents 2–3 to integrate/package. Do not ship a wheel alone as a production release.

## Licensing / provenance guard

The copied `.py`, `.html`, `.css`, `.js` and small example data were fetched from the owner's public VPS_1 repo, using its existing PR branches. No third-party repository was imported. Existing uploader dependency list includes external Python packages with their own licenses; installation and compliance remain separate release checks. The repository does not assert that copy of owner-authored code supplies rights to independent third-party assets. Before production, run dependency license/SBOM and secret scans on the pinned integration SHA.
