"""Offline Chromium check of the real Upload Engine HTML, scripts and styles.

Run explicitly after installing Playwright + Chromium. All browser requests are
fulfilled locally; no live service, uploader worker or remote API is used.
"""
import os, sys, tempfile
from pathlib import Path
repo=Path(__file__).resolve().parents[2]
temp=tempfile.TemporaryDirectory()
for key in ('YOUTUBE_UPLOAD_MANAGER_DATA_DIR','LUM_GOOGLE_OAUTH_ROOT','LUM_AUTO_ENGINE_V6_ROOT'):
 os.environ.pop(key,None)
os.environ.update(LUM_ENGINE_INSTANCE='concat',LUM_DATA_DIR=str(Path(temp.name)/'concat-browser'),LUM_BASE_PATH='/concat-uploader',LUM_SUITE_AUTH='1',CONCAT_WEB_IN_PROCESS='1',LUM_SESSION_SECRET='temporary-browser-test-secret-over-thirty-two',LUM_COOKIE_SECURE='1')
sys.path.insert(0,str(repo/'reused/uploader/part01'))
from fastapi.testclient import TestClient
from lum_engine import standalone_app as web
client=TestClient(web.app,base_url='https://concat.test',headers={'X-Auth-User':'alice','X-App-Access':'concat-studio'})
response=client.get('/concat-uploader/app?tab=add_video')
assert response.status_code==200,response.text
html=response.text
sheets_response=client.get('/concat-uploader/app?tab=sheets')
assert sheets_response.status_code==200,sheets_response.text
sheets_html=sheets_response.text
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
 browser=p.chromium.launch(headless=True,args=['--no-sandbox'])
 page=browser.new_page()
 calls={'sync':0,'save':0}
 state={'sync_error':False}
 def route(request):
  from urllib.parse import urlsplit
  path=urlsplit(request.request.url).path
  if path.endswith('/api/concat/available-media'):
   return request.fulfill(json={'items':[{'id':'video','name':'<Long escaped video name> ' + 'x'*150 + '.mp4','link':'https://concat.test/api/cloud-render/media/video/file'}]})
  if path.endswith('/api/concat/metadata'):
   calls['sync']+=1
   if state['sync_error']:
    return request.fulfill(status=400,json={'detail':'Duplicate video names in metadata sheet'})
   return request.fulfill(json={'ok':True,'video_link':'https://concat.test/api/cloud-render/media/video/file','title':'Sheet title <plain text>','description':'Line 1\nLine 2'})
  if path.endswith('/actions/concat/metadata-sheet'):
   calls['save']+=1
   return request.fulfill(json={'ok':True})
  if path.startswith('/concat-uploader/static/'):
   file=repo/'reused/uploader/part01/static'/Path(path).name
   if file.is_file():
    return request.fulfill(path=str(file))
  if path.endswith('/app'):
   return request.fulfill(body=sheets_html if 'tab=sheets' in request.request.url else html,content_type='text/html')
  return request.fulfill(body='',content_type='text/plain')
 page.route('**/*',route)
 errors=[]
 page.on('pageerror',lambda error:errors.append(str(error)))
 for width in (1440,1024,760,390,320):
  page.set_viewport_size({'width':width,'height':950})
  page.goto('https://concat.test/concat-uploader/app?tab=add_video',wait_until='networkidle')
  page.locator('#concat-media-video option').nth(1).wait_for(state='attached')
  link=page.locator('#concat-media-link').bounding_box()
  select=page.locator('#concat-media-video').bounding_box()
  assert select['x']>=0 and select['x']+select['width']<=width+1,(width,select)
  assert link['x']>=0 and link['x']+link['width']<=width+1,(width,link)
  if width>760:
   assert select['x']>link['x']+link['width'],(width,link,select)
  else:
   assert select['y']>link['y']+link['height'],(width,link,select)
  page.fill('[name=title]','Manual title')
  page.fill('[name=description]','Manual description')
  page.fill('[name=tags]','keep tags')
  page.fill('[name=category_id]','17')
  page.fill('[name=app_upload_at]','2099-01-01T20:00')
  before=calls['sync']
  page.select_option('#concat-media-video',index=1)
  assert calls['sync']==before,'Selection must not fetch metadata'
  assert page.input_value('[name=title]')=='Manual title'
  page.click('[data-concat-metadata-sync]')
  page.wait_for_function("!document.querySelector('[data-concat-metadata-sync]').disabled")
  assert calls['sync']==before+1
  assert page.input_value('[name=title]')=='Sheet title <plain text>'
  assert page.input_value('[name=description]')=='Line 1\nLine 2'
  assert page.input_value('[name=tags]')=='keep tags'
  assert page.input_value('[name=category_id]')=='17'
  assert page.input_value('[name=app_upload_at]')=='2099-01-01T20:00'
  state['sync_error']=True
  page.click('[data-concat-metadata-sync]')
  page.wait_for_function("!document.querySelector('[data-concat-metadata-sync]').disabled")
  assert 'Duplicate' in page.inner_text('[data-concat-metadata-status]')
  assert page.input_value('[name=title]')=='Sheet title <plain text>'
  state['sync_error']=False
  sync_box=page.locator('[data-concat-metadata-sync]').bounding_box()
  assert sync_box['x']>=0 and sync_box['x']+sync_box['width']<=width+1,(width,sync_box)
  assert page.input_value('#concat-media-link')=='https://concat.test/api/cloud-render/media/video/file'
  page.fill('#concat-media-link','https://concat.test/manual')
  page.click('#concat-media-refresh')
  page.wait_for_function("!document.querySelector('#concat-media-refresh').disabled")
  assert page.input_value('#concat-media-link')=='https://concat.test/manual'
  page.goto('https://concat.test/concat-uploader/app?tab=sheets',wait_until='networkidle')
  assert page.locator('[data-concat-metadata-sync]').count()==0
  page.click('[data-concat-metadata-open]')
  dialog=page.locator('[data-concat-metadata-dialog]')
  assert dialog.is_visible()
  box=dialog.bounding_box()
  assert box['x']>=0 and box['x']+box['width']<=width+1,(width,box)
  page.fill('[name=sheet_url]','https://docs.google.com/spreadsheets/d/'+'a'*30+'/edit')
  page.click('[data-concat-metadata-sheet-form] button[type=submit]')
  page.wait_for_function("!document.querySelector('[data-concat-metadata-dialog]').open")
  assert page.inner_text('[data-concat-metadata-open]')=='Edit Metadata Sheet'
  print('PASS real Chromium media and metadata sync/popup layout:',width)
 assert not errors,errors
 browser.close()
client.close()
temp.cleanup()
