"""Local stdio transport to Concat's existing engine. No alternate project writer."""
import collections,json,queue,subprocess,threading,itertools,os
from pathlib import Path

class EngineError(RuntimeError):pass
class McpEngine:
    def __init__(self,executable,timeout=90):
        self.executable=Path(executable).resolve();self.timeout=timeout;self.pending={};self.guard=threading.Lock();self.serial=threading.RLock();self.ids=itertools.count(1);self.logs=collections.deque(maxlen=250)
        if not self.executable.is_file():raise EngineError('Concat engine is missing. Choose the bundled native/concat.exe in Settings.')
        env=os.environ.copy();cpu=self.executable.parent/'cpu-vulkan';manifest=cpu/'lvp_icd.x86_64.json'
        if os.name=='nt' and manifest.is_file():
            env['PATH']=str(cpu)+os.pathsep+env.get('PATH','')
            key='VK_DRIVER_FILES' if env.get('CONCAT_GPU','').lower()=='software' else 'VK_ADD_DRIVER_FILES'
            env.setdefault(key,str(manifest))
        self.process=subprocess.Popen([str(self.executable),'--react-bridge'],cwd=self.executable.parent,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf8',errors='replace',bufsize=1,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        threading.Thread(target=self._read,daemon=True).start();threading.Thread(target=self._stderr,daemon=True).start()
        self.request('initialize',{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'concat-react-desktop','version':'0.1.0'}})
        self.notify('notifications/initialized',{})
        self.tools={t['name']:t for t in self.request('tools/list',{}).get('tools',[])}
    def _stderr(self):
        for line in self.process.stderr:self.logs.append(line.rstrip())
    def _read(self):
        try:
            for line in self.process.stdout:
                try:data=json.loads(line)
                except ValueError:continue
                if 'id' not in data:continue
                with self.guard:target=self.pending.get(data['id'])
                if target:target.put(data)
        finally:
            with self.guard:
                for q in self.pending.values():q.put({'error':{'message':'Concat engine closed unexpectedly.'}})
    def notify(self,method,params):
        with self.serial:self.process.stdin.write(json.dumps({'jsonrpc':'2.0','method':method,'params':params})+'\n');self.process.stdin.flush()
    def request(self,method,params):
        with self.serial:
            if self.process.poll() is not None:raise EngineError('Concat engine is not running.')
            ident=next(self.ids);target=queue.Queue(maxsize=2)
            with self.guard:self.pending[ident]=target
            try:
                self.process.stdin.write(json.dumps({'jsonrpc':'2.0','id':ident,'method':method,'params':params},allow_nan=False)+'\n');self.process.stdin.flush()
                try:reply=target.get(timeout=self.timeout)
                except queue.Empty:raise EngineError('Concat engine timed out. The edit outcome is unknown; refresh before retrying.') from None
                if 'error' in reply:raise EngineError(reply['error'].get('message','Engine refused request.'))
                return reply.get('result',{})
            finally:
                with self.guard:self.pending.pop(ident,None)
    def call(self,name,arguments=None,raw=False):
        if name not in self.tools:raise EngineError('This engine does not expose '+name+'. This build is missing the required engine service.')
        value=self.request('tools/call',{'name':name,'arguments':arguments or {}})
        if value.get('isError'):
            error=value.get('structuredContent',{}).get('error',{}).get('message')
            if not error:error='\n'.join(x.get('text','') for x in value.get('content',[]) if x.get('type')=='text')
            raise EngineError(error or 'Engine rejected the operation.')
        if raw:return value
        if 'structuredContent' in value:return value['structuredContent']
        texts=[x['text'] for x in value.get('content',[]) if x.get('type')=='text']
        if len(texts)==1:
            try:return json.loads(texts[0])
            except ValueError:return texts[0]
        return value
    def close(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            try:self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:self.process.terminate();self.process.wait(timeout=10)
