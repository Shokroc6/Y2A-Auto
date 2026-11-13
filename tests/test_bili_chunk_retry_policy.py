"""Synthetic fault injection; AST executes production methods, no platform traffic."""
import ast, asyncio, pathlib, types, sys, unittest, tempfile, time, random, re
from unittest.mock import patch
ROOT = pathlib.Path(__file__).resolve().parent
SOURCE = ROOT.parent
from modules.bili_sdk.exceptions.NetworkException import NetworkException
from modules.bili_sdk.exceptions.ApiException import ApiException
from modules import upload_errors as err

class Events:
    def __getattr__(self,k):return types.SimpleNamespace(value=k)
class Clock:
    now=0
    def monotonic(self): return self.now
    async def sleep(self,n): self.sleeps.append(n);self.now+=n
    def __init__(self):self.now=0;self.sleeps=[]
class Client:
    def __init__(self,seq,clock,cost=0):self.seq=iter(seq);self.calls=[];self.clock=clock;self.cost=cost
    async def request(self,**kw):
        self.calls.append(kw);self.clock.now+=min(self.cost,kw['timeout']);v=next(self.seq)
        if isinstance(v,BaseException):raise v
        return types.SimpleNamespace(code=v if isinstance(v,int) else v[0],headers={} if isinstance(v,int) else v[1],utf8_text=lambda:'MULTIPART_PUT_SUCCESS')
ns=dict(re=re,asyncio=asyncio,create_task=asyncio.create_task,CancelledError=asyncio.CancelledError,ApiException=ApiException,NetworkException=NetworkException,VideoUploaderPage=object,VideoUploaderEvents=Events(),time=time,random=random,_chunk_put_timeout=lambda n:160)
cl=next(n for n in ast.parse((SOURCE/'modules/bili_sdk/video_uploader.py').read_text()).body if isinstance(n,ast.ClassDef) and n.name=='VideoUploader')
for node in cl.body:
    if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name in ['_upload_chunk','_upload_page']:
        exec(compile(ast.Module(body=[node],type_ignores=[]),'<production-AST>','exec'),ns)
class U:
    line=None
    _upload_chunk=ns['_upload_chunk'];_upload_page=ns['_upload_page']
    def __init__(self):self.events=[]
    def dispatch(self,k,v):self.events.append((k,v))
    def _switch_upload_endpoint(self,p,l):return p
    def _get_upload_url(self,p):return 'https://offline.invalid/fake?PRIVATE'
class RetryTests(unittest.TestCase):
    def run_case(self,seq,cost=0):
        clock=Clock();client=Client(seq,clock,cost);ns['get_client']=lambda:client;u=U()
        with tempfile.TemporaryDirectory() as tmp:
            f=pathlib.Path(tmp)/'fixture';f.write_bytes(b'fixture')
            p=types.SimpleNamespace(path=str(f),get_size=lambda:7)
            pre=dict(chunk_size=10,upload_id='PRIVATE',auth='PRIVATE',threads=3)
            with patch.object(time,'monotonic',clock.monotonic),patch.object(asyncio,'sleep',clock.sleep),patch.object(random,'uniform',side_effect=lambda a,b:b):
                try: result=asyncio.run(u._upload_chunk(p,0,0,1,pre))
                except Exception as e:result=e
        return result,client,clock,u
    def test_mixed_six_total_attempts(self):
        r,c,t,u=self.run_case([Exception('curl: (28)'),Exception('curl: (6)'),503,Exception('curl: (6)'),503,200])
        self.assertIsInstance(r,dict);self.assertEqual(len(c.calls),6)
        self.assertEqual(t.sleeps,[5,10,20,40,60]);self.assertTrue(all(x['params']==c.calls[0]['params'] and x['data']==b'fixture' for x in c.calls))
        self.assertNotIn('PRIVATE',str(u.events))
    def test_permanent_and_unknown_stop(self):
        for failure in [403,Exception('curl: (60)'),TypeError('PRIVATE')]:
            with self.subTest(failure=type(failure).__name__):
                r,c,t,u=self.run_case([failure,200]);self.assertIsInstance(r,Exception);self.assertEqual(len(c.calls),1)
    def test_budget_covers_requests_and_waits(self):
        r,c,t,u=self.run_case([TimeoutError()]*10,cost=150)
        self.assertIsInstance(r,Exception);self.assertLessEqual(t.now,900)
        self.assertLess(c.calls[-1]['timeout'],160)
        self.assertEqual(u.events[-1][1]['reason'],'deadline_exhausted')
    def test_retry_after_not_early(self):
        for header in ['120','Fri, 01 Jan 2100 00:00:00 GMT']:
            r,c,t,u=self.run_case([(429,{'rEtRy-AfTeR':header}),200])
            if header=='120':self.assertEqual(t.sleeps,[120]);self.assertIsInstance(r,dict)
            else:self.assertEqual(len(c.calls),1);self.assertEqual(u.events[-1][1]['reason'],'deadline_exhausted')
    def test_cancel_bridge_exists_and_cleans_children(self):
        tree=ast.parse((SOURCE/'modules/bilibili_uploader.py').read_text())
        node=next((n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='_start_cancellable_upload'),None)
        self.assertIsNotNone(node,'task cancel flag currently does not reach SDK')
        env={'asyncio':asyncio};exec(compile(ast.Module(body=[node],type_ignores=[]),'<bridge-AST>','exec'),env)
        async def check():
            import threading
            flag=threading.Event();clean=[]
            class Upload:
                async def start(self):
                    try:await asyncio.sleep(20)
                    except asyncio.CancelledError:return None  # real SDK start swallows cancellation
                    finally:clean.append(True)
            task=asyncio.create_task(env['_start_cancellable_upload'](Upload(),flag))
            await asyncio.sleep(.02);flag.set()
            with self.assertRaises(asyncio.CancelledError):await asyncio.wait_for(task,1)
            self.assertEqual(clean,[True]);self.assertEqual(len(asyncio.all_tasks()),1)
        asyncio.run(check())
    def test_structured_curl_code_precedes_message(self):
        # Use the real exception type when available (target image).
        try:from curl_cffi import CurlError
        except ImportError:raise AssertionError('curl_cffi is required by project requirements')
        self.assertEqual(err.curl_error_code(CurlError('curl: (60) PRIVATE',6)),6)
        generic=Exception('unknown');generic.code=6
        self.assertIsNone(err.curl_error_code(generic))
    def test_classification_matrix(self):
        for code in [6,7,16,18,28,35,52,55,56,92]:
            r,c,t,u=self.run_case([Exception(f'curl: ({code})'),200]);self.assertIsInstance(r,dict)
        for code in [408,429,500,502,503,504]:
            r,c,t,u=self.run_case([code,200]);self.assertIsInstance(r,dict)
        for code in [400,401,403,404,405,409,412,413,501,505]:
            r,c,t,u=self.run_case([code,200]);self.assertEqual(len(c.calls),1)
    def test_exhaustion_and_retry_after_values(self):
        r,c,t,u=self.run_case([503]*8);self.assertEqual(len(c.calls),6);self.assertEqual(u.events[-1][1]['reason'],'attempts_exhausted')
        for value in ['0','bad','-10','Sun, 06 Nov 1994 08:49:37 GMT','999999999999999999999999999999999999999999']:
            r,c,t,u=self.run_case([(503,{'Retry-After':value}),200])
            self.assertEqual(len(c.calls),1 if value.startswith('999') else 2)
        with patch.object(time,'time',return_value=0):
            r,c,t,u=self.run_case([(503,{'Retry-After':'Thu, 01 Jan 1970 00:02:00 GMT'}),200]);self.assertEqual(t.sleeps,[120])
    def test_cancel_request_and_backoff(self):
        async def check(backoff):
            with tempfile.TemporaryDirectory() as tmp:
                f=pathlib.Path(tmp)/'fixture';f.write_bytes(b'a');p=types.SimpleNamespace(path=str(f),get_size=lambda:1)
                entered=asyncio.Event();clean=[]
                class BlockClient:
                    async def request(self,**kw):
                        if backoff:return types.SimpleNamespace(code=503,headers={},utf8_text=lambda:'')
                        entered.set()
                        try:await asyncio.sleep(20)
                        finally:clean.append(True)
                ns['get_client']=lambda:BlockClient();u=U()
                async def sleeper(n):
                    entered.set();await real_sleep(20)
                real_sleep=asyncio.sleep
                with patch.object(asyncio,'sleep',sleeper if backoff else real_sleep):
                    task=asyncio.create_task(u._upload_chunk(p,0,0,1,dict(chunk_size=1,upload_id='PRIVATE',auth='PRIVATE')))
                    await entered.wait();task.cancel()
                    with self.assertRaises(asyncio.CancelledError):await task
                if not backoff:self.assertEqual(clean,[True])
                self.assertEqual(len(asyncio.all_tasks()),1)
        for mode in [False,True]:asyncio.run(check(mode))
    def test_batch_success_not_resent_and_failure_cancels(self):
        async def check():
            u=U();started=[];done=[];cancelled=[];complete=[]
            async def pre(p):return dict(chunk_size=1,upload_id='PRIVATE',threads=3)
            async def chunk(p,offset,*a):
                started.append(offset)
                if offset==0:done.append(offset);return
                if offset==1:await asyncio.sleep(.01);raise TypeError('PRIVATE')
                try:await asyncio.sleep(20)
                except asyncio.CancelledError:cancelled.append(offset);raise
            async def finish(*a):complete.append(True)
            u._preupload=pre;u._upload_chunk=chunk;u._complete_page=finish
            with self.assertRaises(TypeError):await u._upload_page(types.SimpleNamespace(get_size=lambda:7))
            self.assertEqual(started,[0,1,2]);self.assertEqual(done,[0]);self.assertEqual(cancelled,[2]);self.assertFalse(complete)
        asyncio.run(check())
    def test_cancel_does_not_discard_confirmed_submit(self):
        tree=ast.parse((SOURCE/'modules/bilibili_uploader.py').read_text());node=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='_start_cancellable_upload')
        env={'asyncio':asyncio};exec(compile(ast.Module(body=[node],type_ignores=[]),'<bridge-AST>','exec'),env)
        async def check():
            import threading
            flag=threading.Event()
            class Upload:
                async def start(self):flag.set();return {'bvid':'BV-confirmed'}
            self.assertEqual(await env['_start_cancellable_upload'](Upload(),flag),{'bvid':'BV-confirmed'})
        asyncio.run(check())
    def test_unchanged_nonchunk_requests_and_cancel_entry_wiring(self):
        # Freeze the unaffected 1.1.2 request methods without a git dependency.
        import hashlib
        baseline_hashes = {'_preupload': '8be11c3f7738f945a60168cee91821ab32d709b0e67b6c9ea20bfa4f7c6cb437', '_upload_cover': '7935df7010ea67184800dcc14ceec5b7a58597b9fb7c5f99518bd1585b3020f5', '_complete_page': '393560ed9b2f0d21aed332d66c8e25de26d663b52f0e91a294a3899e4f35f877', '_submit': 'a40e90b6ac56af94774095e311209346a8e426670510aecfcd7e45ba0afb5d5b'}
        for name, expected in baseline_hashes.items():
            after=next(n for n in cl.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name==name)
            self.assertEqual(hashlib.sha256(ast.get_source_segment((SOURCE/'modules/bili_sdk/video_uploader.py').read_text(), after).encode()).hexdigest(), expected, name)
        t=ast.parse((SOURCE/'modules/task_manager.py').read_text())
        processor=next(n for n in t.body if isinstance(n,ast.ClassDef) and n.name=='TaskProcessor')
        method=next(n for n in processor.body if isinstance(n,ast.FunctionDef) and n.name=='_do_upload_to_bilibili')
        call=next(n for n in ast.walk(method) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='upload_video')
        self.assertIn('cancel_event',[k.arg for k in call.keywords])
        force=next(n for n in t.body if isinstance(n,ast.FunctionDef) and n.name=='force_upload_task')
        self.assertTrue(any(isinstance(n,ast.Attribute) and n.attr=='_upload_to_target' for n in ast.walk(force)))
    def test_active_reaper_requests_existing_cancel_flag(self):
        import threading
        source=ast.parse((SOURCE/'modules/task_manager.py').read_text())
        env={'threading':threading,'_TASK_CANCEL_LOCK':threading.Lock(),'_TASK_CANCEL_FLAGS':{},'time':time,
             'logger':types.SimpleNamespace(warning=lambda *a:None,info=lambda *a:None,error=lambda *a:None),
             'PROCESSING_STATES':['uploading'],'_is_task_active':lambda tid:True}
        class Connection:
            def execute(self,*a):return self
            def fetchall(self):return [('fixture','uploading','2000-01-01')]
            def commit(self):pass
            def close(self):pass
        env['get_db_connection']=Connection
        for name in ['get_task_cancel_event','request_task_cancel','reset_stuck_tasks']:
            node=next(n for n in source.body if isinstance(n,ast.FunctionDef) and n.name==name)
            exec(compile(ast.Module(body=[node],type_ignores=[]),'<reaper-AST>','exec'),env)
        flag=env['get_task_cancel_event']('fixture')
        self.assertEqual(env['reset_stuck_tasks'](skip_active=True,cancel_active=True),0)
        self.assertTrue(flag.is_set())
    def test_success_stops_immediately(self):
        r,c,t,u=self.run_case([200,503]);self.assertEqual(len(c.calls),1);self.assertFalse(t.sleeps)
if __name__=='__main__':unittest.main(verbosity=2)
