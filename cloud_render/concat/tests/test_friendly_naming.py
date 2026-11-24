"""Friendly names propagate as metadata; unique job paths stay unchanged."""
import hashlib
import http.client
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile
from cloud_render.concat.hybrid.naming import (
    basename,audio_stem,zip_name,mp4_name,numbered,project_names,content_disposition,
)
from cloud_render.concat.gpu.worker import RenderWorker,Handler,SaladDualStackHTTPServer

MP4=b'\x00\x00\x00\x18ftypisom'+b'\0'*80
TOKEN='n'*48
class NamingTests(unittest.TestCase):
    def test_cross_platform_paths_reserved_names_and_unicode_limits(self):
        self.assertEqual(audio_stem(r'C:\Voices\Alex.final.v2.mp3'),'Alex.final.v2')
        self.assertEqual(audio_stem('/voice/آواز test.wav'),'آواز test')
        for name in ['CON','con.txt','COM¹','LPT9','AUX','CONOUT$']:
            self.assertTrue(basename(name).startswith('_'))
        self.assertEqual(basename(' a<b>:c"d/e\\f|g?h*i\n '),'a_b__c_d_e_f_g_h_i')
        self.assertEqual(basename('... '),'Untitled')
        for value in [basename('🎾'*100),numbered('🎾'*100,4294967295)]:
            self.assertLessEqual(len(value.encode('utf-16-le'))//2,120)
        self.assertEqual(mp4_name('tennis.MP4'),'tennis.mp4')
        self.assertEqual(zip_name('tennis.zip'),'tennis.zip')
    def test_display_name_override_and_rename_remain_independent(self):
        doc={'name':'My New Video'}
        self.assertEqual(project_names(doc)['output_filename'],'My New Video.mp4')
        doc['name']='My Renamed Video'
        self.assertEqual(project_names(doc)['bundle_filename'],'My Renamed Video.zip')
        doc['outputBasename']='Manual: Final'
        names=project_names(doc)
        self.assertEqual(names['project_name'],'My Renamed Video')
        self.assertEqual(names['output_filename'],'Manual_ Final.mp4')
        self.assertEqual(project_names({'timelines':[{'id':'t1','name':'Timeline Name'}]})['output_basename'],'Timeline Name')
    def test_headers_are_ascii_and_cannot_inject_newlines(self):
        header=content_disposition('آواز\r\n".mp4')
        self.assertTrue(header.isascii());self.assertNotIn('\r',header);self.assertNotIn('\n',header)
        self.assertIn("filename*=UTF-8''",header)
    def test_embedded_powershell_mirrors_remain_identical(self):
        root=Path(__file__).resolve().parents[3]
        self.assertEqual((root/'concat_gpu/concat_gpu_client.ps1').read_bytes(),
            (root/'upstream/src/crates/concat/src/concat_gpu_client.ps1').read_bytes())

class WorkerNameTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.worker=RenderWorker(Path(self.temp.name),token=TOKEN,renderer=lambda _,out:out.write_bytes(MP4))
    def tearDown(self):
        self.worker.shutdown();self.temp.cleanup()
    def submit(self,name,override=None):
        doc={'name':name,'media':[]}
        if override is not None:doc['outputBasename']=override
        with io.BytesIO() as stream:
            with zipfile.ZipFile(stream,'w') as z:
                z.writestr('bundle.json',json.dumps({'version':1,'project_file':'concat.json','references':[]}))
                z.writestr('project/concat.json',json.dumps(doc))
            data=stream.getvalue()
        self.worker.accept('unique_job_id',io.BytesIO(data),len(data),hashlib.sha256(data).hexdigest())
        self.worker.shutdown()
        return self.worker.status('unique_job_id')
    def test_saved_name_is_metadata_and_internal_paths_stay_unique(self):
        status=self.submit('My Voice.final')
        self.assertEqual(status['output_filename'],'My Voice.final.mp4')
        self.assertEqual(status['bundle_filename'],'My Voice.final.zip')
        self.assertTrue((self.worker.outputs/'unique_job_id.mp4').is_file())
        self.assertTrue((self.worker.inputs/'unique_job_id/project.zip').is_file())
    def test_delivery_uses_same_authenticated_job_url_and_encodes_friendly_name(self):
        self.submit('آواز: Match Highlights')
        self.worker.receiver='https://receiver.example'
        self.worker.receiver_token='receiver-test-token'
        captured=[]
        class Opener:
            def open(inner,request,timeout):
                captured.append(request)
                return io.BytesIO(b'{"ok":true,"state":"ready"}')
        with patch('cloud_render.concat.gpu.worker.urllib.request.build_opener',return_value=Opener()):
            self.worker._deliver('unique_job_id',self.worker.outputs/'unique_job_id.mp4')
        from urllib.parse import urlsplit,parse_qs
        request=captured[0];url=urlsplit(request.full_url);query=parse_qs(url.query)
        self.assertEqual(url.path,'/api/cloud-render/ingest/unique_job_id/chunk')
        self.assertEqual(query['name'],['آواز_ Match Highlights.mp4'])
        self.assertEqual(request.get_header('X-cloud-render-token'),'receiver-test-token')
        self.assertEqual(request.data,MP4)
    def test_manual_override_and_http_download_suggestion(self):
        status=self.submit('Canonical Project','آواز: Final')
        bound=type('NamingHandler',(Handler,),{'worker':self.worker})
        server=SaladDualStackHTTPServer(('::',0),bound)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
            conn.request('GET','/api/jobs/unique_job_id/output',headers={'Authorization':'Bearer '+TOKEN})
            response=conn.getresponse();self.assertEqual(response.status,200)
            self.assertEqual(response.getheader('Content-Disposition'),content_disposition(status['output_filename']))
            self.assertEqual(response.read(),MP4);conn.close()
        finally:server.shutdown();server.server_close();thread.join(timeout=5)
