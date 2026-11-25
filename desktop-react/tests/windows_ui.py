"""Actual WebView2 UI interactions against the compiled native bridge on Windows."""
import sys,tempfile,threading,time,json,traceback
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from host.api import Api,public_api

def main():
    if sys.platform!='win32':raise RuntimeError('Windows required')
    import webview
    from PIL import Image,ImageDraw,ImageGrab
    root=Path(__file__).resolve().parents[1];report=root/'dist/windows-ui.json';report.parent.mkdir(exist_ok=True)
    checks=[];errors=[]
    with tempfile.TemporaryDirectory(prefix='concat-react-webview-') as folder:
        api=Api(settings=Path(folder)/'preferences.json');api._discover()
        result=api.new_project('WebView test',folder);assert result['ok'],result
        pic=Path(folder)/'picture.png';image=Image.new('RGB',(640,360),'#dceaff');ImageDraw.Draw(image).ellipse((150,40,490,340),fill='#3979ef');image.save(pic)
        api._call('media_import',{'path':api.path,'file':str(pic)});api._refresh();mid=api.view['project']['media'][0]['id'];api.add_media([mid],0)
        window=webview.create_window('Concat React integration',str(root/'frontend/dist/index.html'),js_api=public_api(api),width=1500,height=960);api.window=window
        def wait(fn,label):
            deadline=time.monotonic()+45
            while time.monotonic()<deadline:
                if fn():checks.append(label);print('PASS',label,flush=True);return
                time.sleep(.2)
            raise AssertionError('Timed out: '+label)
        def inspect():
            try:
                wait(lambda:window.evaluate_js('Boolean(document.querySelector(".timeline-clip"))'),'React workspace and native project boot')
                window.evaluate_js('document.querySelector(".timeline-clip").click()')
                wait(lambda:'picture' in (window.evaluate_js('document.querySelector(".inspector .panel-head").textContent') or '').lower(),'Timeline selection reaches inspector')
                window.evaluate_js('Array.from(document.querySelectorAll("[role=tab]")).find(x=>x.textContent==="Physics").click()')
                wait(lambda:window.evaluate_js('Array.from(document.querySelectorAll(".preset")).some(x=>x.textContent.includes("Spring"))'),'Native physics catalogue visible')
                window.evaluate_js('Array.from(document.querySelectorAll(".preset")).find(x=>x.textContent.includes("Spring")).click()')
                window.evaluate_js('Array.from(document.querySelectorAll("button")).find(x=>x.textContent==="Apply to selected").click()')
                wait(lambda:bool(api._active()['clips'][0].get('keys')),'React preset action commits real native keys')
                wait(lambda:window.evaluate_js('Boolean(document.querySelector(".video-canvas img"))'),'Native composited preview visible')
                assert window.evaluate_js('document.querySelectorAll("[role=separator]").length')>=2
                checks.append('Resizable workbench separators present')
                ImageGrab.grab().save(root/'dist/windows-editor.png')
                window.evaluate_js('Array.from(document.querySelectorAll("header button")).find(x=>x.textContent.includes("Settings")).click()')
                wait(lambda:'Installed and available models' in (window.evaluate_js('document.body.innerText') or ''),'Native settings and model services inside React drawer')
                ImageGrab.grab().save(root/'dist/windows-settings.png')
            except Exception as e:
                errors.append(str(e));print(traceback.format_exc(),flush=True)
                try:ImageGrab.grab().save(root/'dist/windows-failure.png')
                except Exception:pass
            finally:
                try:api.shutdown()
                finally:window.destroy()
        def timeout():
            import os
            report.write_text(json.dumps({'real_checks':checks,'errors':['WebView2 integration timed out']},indent=2));os._exit(1)
        watchdog=threading.Timer(150,timeout);watchdog.daemon=True;watchdog.start()
        window.events.loaded+=lambda:threading.Thread(target=inspect,daemon=True).start()
        webview.start(gui='edgechromium',http_server=True)
        watchdog.cancel();report.write_text(json.dumps({'real_checks':checks,'errors':errors},indent=2))
    assert not errors,errors
if __name__=='__main__':main()
