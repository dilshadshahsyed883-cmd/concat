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
import io
from PIL import Image
png=io.BytesIO(); Image.new('RGB',(120,68),'red').save(png,format='PNG'); png=png.getvalue()
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
 browser=p.chromium.launch(headless=True,args=['--no-sandbox'])
 page=browser.new_page()
 calls={'sync':0,'save':0,'uploads':0,'queue':0}
 pending_upload=[]
 pending_queue=[]
 items=[{'id':'a'*32,'name':'u1.png','size':len(png),'sha256':'a'*64,'in_use':False,'url':'/concat-uploader/api/concat/thumbnails/'+'a'*32+'/file'}]
 state={'sync_error':False,'hold_upload':False,'hold_queue':False}
 def route(request):
  from urllib.parse import urlsplit
  path=urlsplit(request.request.url).path
  if path.endswith('/api/concat/thumbnails'):
   return request.fulfill(json={'ok':True,'items':items})
  if '/api/concat/thumbnails/' in path and path.endswith('/file'):
   return request.fulfill(body=png,content_type='image/png')
  if path.endswith('/api/concat/thumbnails/add'):
   calls['uploads']+=1
   if state['hold_upload']:
    pending_upload.append(request); return
   if b'bad.png' in request.request.post_data_buffer:
    return request.fulfill(status=400,json={'detail':'Damaged image'})
   return request.fulfill(json={'ok':True,'id':'b'*32})
  if path.endswith('/api/concat/thumbnails/delete'):
   items.clear()
   return request.fulfill(json={'ok':True,'deleted':['a'*32],'errors':[]})
  if path.endswith('/actions/add-video'):
   calls['queue']+=1
   if state['hold_queue']:
    pending_queue.append(request); return
   return request.fulfill(json={'ok':True,'project_id':77,'queue_url':'/concat-uploader/app?tab=queue'})
  if path.endswith('/api/concat/available-media'):
   return request.fulfill(json={'items':[{'id':'video','name':'<Long escaped video name> ' + 'x'*150 + '.mp4','link':'https://concat.test/api/cloud-render/media/video/file'}]})
  if path.endswith('/api/concat/metadata'):
   calls['sync']+=1
   if state['sync_error']:
    return request.fulfill(status=400,json={'detail':'Duplicate video names in metadata sheet'})
   return request.fulfill(json={'ok':True,'video_link':'https://concat.test/api/cloud-render/media/video/file','title':'Sheet title <plain text>','description':'Line 1\nLine 2','thumbnail':items[0] if items else None,'thumbnail_message':'Thumbnail matched' if items else 'No matching thumbnail'})
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
  items[:]=[{'id':'a'*32,'name':'u1.png','size':len(png),'sha256':'a'*64,'in_use':False,'url':'/concat-uploader/api/concat/thumbnails/'+'a'*32+'/file'}]
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
  page.locator('[data-concat-thumbnail-preview]').wait_for(state='visible')
  assert page.input_value('[data-library-thumbnail-id]')=='a'*32
  page.set_input_files('#concat-thumbnail',{'name':'manual.png','mimeType':'image/png','buffer':png})
  assert page.input_value('[data-library-thumbnail-id]')==''
  page.locator('[data-concat-thumbnail-preview]').wait_for(state='visible')
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
  page.click('[data-thumbnail-library-open]')
  popup=page.locator('[data-thumbnail-library]')
  box=popup.bounding_box()
  assert box['x']>=0 and box['x']+box['width']<=width+1,(width,box)
  page.locator('[data-thumbnail-library-grid] .concat-thumbnail-tile').wait_for()
  page.on('dialog',lambda d:d.accept()) if width==1440 else None
  state['hold_upload']=True
  before_upload=calls['uploads']
  page.set_input_files('[data-thumbnail-bulk-files]',[
   {'name':'u1.png','mimeType':'image/png','buffer':png},
   {'name':'bad.png','mimeType':'image/png','buffer':b'bad'},
   {'name':'u2.png','mimeType':'image/png','buffer':png}])
  page.wait_for_function("document.querySelector('[data-thumbnail-upload-progress]').textContent.includes('Validating and saving')")
  assert len(pending_upload)==1
  assert page.locator('[data-thumbnail-upload-progress] progress').count()==3
  page.click('[data-thumbnail-library-close]')
  page.fill('[name=title]','Edited while thumbnails upload')
  page.click('[data-thumbnail-library-open]')
  assert '0 / 3' in page.inner_text('[data-thumbnail-upload-progress]')
  state['hold_upload']=False
  pending_upload.pop().fulfill(json={'ok':True,'id':'b'*32})
  page.wait_for_function("document.querySelector('[data-thumbnail-upload-progress]').textContent.includes('3 / 3 completed')")
  assert calls['uploads']==before_upload+3
  assert 'Damaged image' in page.inner_text('[data-thumbnail-upload-progress]')
  assert 'Saved' in page.inner_text('[data-thumbnail-upload-progress]')
  page.locator('[data-thumbnail-library-grid] button',has_text='Use').click()
  assert page.input_value('[data-library-thumbnail-id]')=='a'*32
  page.locator('[data-concat-thumbnail-preview]').wait_for(state='visible')
  page.click('[data-thumbnail-library-open]')
  page.locator('[data-thumbnail-choice]').check()
  page.click('[data-thumbnail-delete-selected]')
  page.wait_for_function("document.querySelector('[data-thumbnail-library-status]').textContent.includes('deleted')")
  assert page.input_value('[data-library-thumbnail-id]')==''
  page.click('[data-thumbnail-library-close]')
  # Real submit handler, native form validity, spinner, unknown outcome and duplicate-click guard.
  page.fill('#concat-media-link','https://concat.test/api/cloud-render/media/video/file')
  page.evaluate("""() => {
   const g=document.querySelector('[name=group_name]'); g.add(new Option('Owned','Owned')); g.value='Owned';
   const c=document.querySelector('[name=channel_db_id]'); c.add(new Option('Owner channel','1')); c.disabled=false; c.value='1';
  }""")
  state['hold_queue']=True
  before_queue=calls['queue']
  page.click('[data-concat-add-queue]')
  page.wait_for_function("document.querySelector('[data-concat-add-queue]').disabled")
  assert page.locator('[data-concat-add-queue]').evaluate("e => e.classList.contains('concat-processing')")
  page.wait_for_function("document.querySelector('[data-concat-queue-status]').textContent.includes('Verifying')")
  page.evaluate("document.querySelector('[data-concat-add-queue]').form.requestSubmit()")
  assert calls['queue']==before_queue+1
  pending_queue.pop().fulfill(status=400,json={'ok':False,'detail':'Fixture admission rejected'})
  page.wait_for_function("!document.querySelector('[data-concat-add-queue]').disabled")
  assert 'Fixture admission rejected' in page.inner_text('[data-concat-queue-status]')
  assert page.input_value('[name=title]')=='Edited while thumbnails upload'
  state['hold_queue']=False
  page.click('[data-concat-add-queue]')
  page.wait_for_url('**/app?tab=queue')
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
