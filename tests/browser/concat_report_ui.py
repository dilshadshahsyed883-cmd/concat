"""Offline real Chromium report/download and saved Drive sync/mobile checks."""
import os
from pathlib import Path
import sys
import tempfile
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright
from app.auth import Principal
from app.main import create_app
from app.modules.concat.tenant import get_registry


with tempfile.TemporaryDirectory() as tmp:
    app = create_app()
    app.state.concat_data_root = tmp
    principal = Principal('report-test', is_active=True, concat_granted=True)
    app.state.identity_resolver = lambda request: principal
    client = TestClient(app, base_url='https://concat.test')
    registry = get_registry(app)
    manager = registry.for_principal(principal)
    manager.production.close()  # fixture tests scanning, never real Drive/GPU dispatch
    manager.metrics.event('job', {'average_fps': 42, 'speed_multiplier': 1.4}, job=1)
    hold_sync = threading.Event()
    release_sync = threading.Event()
    discover_calls = []
    def discover(link):
        discover_calls.append(link)
        if hold_sync.is_set():
            assert release_sync.wait(15), 'test did not release pending sync'
        return [('drive-ui-one', 'one.zip', '', 0, '', 'saved-ui-folder', 'one.zip')]
    manager.production.drive.discover = discover
    # Use real HTTP for attachments. Chromium can cancel intercepted/fulfilled
    # attachment requests even when route.fulfill supplies the correct headers.
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.reply(client.get(self.path))

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            headers = {key: self.headers[key] for key in ('Content-Type', 'X-Cloud-Render-CSRF', 'Origin', 'Referer') if key in self.headers}
            self.reply(client.post(self.path, content=body, headers=headers))

        def reply(self, response):
            self.send_response(response.status_code)
            for key, value in response.headers.items():
                if key.lower() not in ('content-length', 'content-encoding', 'transfer-encoding', 'connection', 'set-cookie'):
                    self.send_header(key, value)
            self.send_header('Content-Length', str(len(response.content)))
            self.end_headers()
            self.wfile.write(response.content)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    origin = f'http://127.0.0.1:{server.server_port}'
    app.state.settings = replace(app.state.settings, origin=origin)
    try:
        with sync_playwright() as p:
            launch = {'headless': True, 'args': ['--no-sandbox']}
            if os.getenv('CHROMIUM_EXECUTABLE'):
                launch['executable_path'] = os.environ['CHROMIUM_EXECUTABLE']
            browser = p.chromium.launch(**launch)
            page = browser.new_page(accept_downloads=True)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('**/*', lambda route: route.continue_()
                       if route.request.url.startswith(origin + '/') else route.abort())
            for width in (1440, 760, 390, 320):
                page.set_viewport_size({'width': width, 'height': 950})
                page.goto(origin + '/concat/gpu-config', wait_until='networkidle')
                link = page.locator('#cr-download-report')
                link.scroll_into_view_if_needed()
                box = link.bounding_box()
                assert box and box['x'] >= 0 and box['x'] + box['width'] <= width + 1, (width, box)
                page.select_option('#cr-report-hours', '168')
                assert link.get_attribute('href').endswith('?hours=168')
                with page.expect_download() as pending:
                    link.click()
                download = pending.value
                assert download.suggested_filename == 'concat-performance.txt', download.suggested_filename
                text = Path(download.path()).read_text()
                assert 'Requested range: 168 hours' in text
                assert '"average_fps": 42' in text
                assert page.locator('#cr-resource-dialog').count() == 0
                before = len(discover_calls)
                page.goto(origin + '/concat/production', wait_until='networkidle')
                page.locator('#cr-edit-drive-source').wait_for(state='visible')
                page.click('#cr-edit-drive-source')
                page.locator('#cr-drive-source-dialog').wait_for(state='visible')
                page.fill('#cr-saved-drive-link', 'https://drive.google.com/drive/folders/saved-ui-folder')
                page.click('#cr-save-drive-source')
                page.locator('#cr-drive-source-dialog').wait_for(state='hidden')
                assert len(discover_calls) == before, 'save must not trigger Drive scan'
                for selector in ('#cr-edit-drive-source', '#cr-sync-drive'):
                    box = page.locator(selector).bounding_box()
                    assert box and box['x'] >= 0 and box['x'] + box['width'] <= width + 1, (width, selector, box)
                page.fill('#cr-drive-link', 'https://drive.google.com/file/d/manual-ui/view')
                hold_sync.set(); release_sync.clear()
                page.click('#cr-sync-drive')
                page.wait_for_function("document.getElementById('cr-sync-drive').textContent.includes('Syncing')")
                assert page.locator('#cr-sync-drive').is_disabled()
                assert page.locator('#cr-scan').is_disabled()
                page.evaluate('window.syncResponsive=false; setTimeout(()=>{window.syncResponsive=true;},20)')
                page.wait_for_function('window.syncResponsive === true')
                release_sync.set(); hold_sync.clear()
                page.wait_for_function("!document.getElementById('cr-sync-drive').disabled")
                assert 'duplicates' in page.locator('#cr-drive-source-state').inner_text()
                assert page.input_value('#cr-drive-link').endswith('/manual-ui/view')
                page.click('#cr-sync-drive')
                page.wait_for_function("document.getElementById('cr-drive-source-state').textContent.includes('0 added · 1 duplicates')")
                page.click('#cr-edit-drive-source')
                page.locator('#cr-drive-source-dialog').wait_for(state='visible')
                assert page.input_value('#cr-saved-drive-link').endswith('/saved-ui-folder')
                page.keyboard.press('Escape')
                page.locator('#cr-drive-source-dialog').wait_for(state='hidden')
            assert not errors, errors
            browser.close()
        print('PASS: TXT downloads, saved Drive popup/sync/dedup/responsiveness and 1440/760/390/320px layouts')
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)
        registry.close()
        client.close()
