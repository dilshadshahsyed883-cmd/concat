# SPDX-License-Identifier: AGPL-3.0-or-later
"""Concat React desktop; the retained native engine owns every edit and rendered pixel."""
import sys
from pathlib import Path

def main():
    import webview
    from host.api import Api,public_api
    api=Api();base=Path(getattr(sys,'_MEIPASS',Path(__file__).resolve().parent));page=base/'frontend/dist/index.html'
    if not page.exists():raise RuntimeError('Frontend missing. Run npm ci && npm run build inside desktop-react/frontend.')
    window=webview.create_window('Concat React Preview',str(page),js_api=public_api(api),width=1500,height=960,min_size=(1050,740),confirm_close=True,background_color='#f3f6fb');api.window=window
    window.events.closing+=api.shutdown
    webview.start(gui='edgechromium' if sys.platform=='win32' else None,http_server=True)
if __name__=='__main__':
    try:main()
    except Exception as e:
        if sys.platform=='win32':
            import ctypes;ctypes.windll.user32.MessageBoxW(0,str(e),'Concat React startup',0x10)
        else:print(str(e),file=sys.stderr)
        sys.exit(1)
