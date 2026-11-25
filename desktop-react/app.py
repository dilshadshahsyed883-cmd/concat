# SPDX-License-Identifier: AGPL-3.0-or-later
"""Concat React desktop; the retained native engine owns every edit and rendered pixel."""
import sys,os
from pathlib import Path

def main():
    import webview
    from host.api import Api,public_api
    api=Api();base=Path(getattr(sys,'_MEIPASS',Path(__file__).resolve().parent));page=base/'frontend/dist/index.html'
    if not page.exists():raise RuntimeError('Frontend missing. Run npm ci && npm run build inside desktop-react/frontend.')
    window=webview.create_window('Concat React Preview',str(page),js_api=public_api(api),width=1500,height=960,min_size=(1050,740),confirm_close=False,background_color='#f3f6fb');api.window=window
    window.events.closing+=api.shutdown
    # Explicit CI handoff: the frozen executable must boot its bundled native
    # services and render the React workspace, not merely create a window.
    if os.environ.get('CONCAT_STARTUP_REPORT'):
        import threading,time
        from host.api import save_json
        def verify_startup():
            report=Path(os.environ['CONCAT_STARTUP_REPORT'])
            deadline=time.monotonic()+100
            try:
                while time.monotonic()<deadline:
                    if getattr(api,'boot_complete',False) and window.evaluate_js('Boolean(document.querySelector("header"))'):
                        if api.engine.process.poll() is not None:raise RuntimeError('Bundled native engine exited')
                        save_json(report,{'status':'passed','frozen':bool(getattr(sys,'frozen',False)),'native_boot':True,'react_workspace':True})
                        return
                    time.sleep(.25)
                raise TimeoutError('Packaged React workspace/native boot timed out')
            except Exception as e:save_json(report,{'status':'failed','error':str(e)})
        window.events.loaded+=lambda:threading.Thread(target=verify_startup,daemon=True).start()
    webview.start(gui='edgechromium' if sys.platform=='win32' else None,http_server=True)
if __name__=='__main__':
    try:main()
    except Exception as e:
        if sys.platform=='win32':
            import ctypes;ctypes.windll.user32.MessageBoxW(0,str(e),'Concat React startup',0x10)
        else:print(str(e),file=sys.stderr)
        sys.exit(1)
