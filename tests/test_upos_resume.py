"""Private local checkpoint behavior. No account credentials/platform traffic."""
import tempfile
import unittest
from pathlib import Path


class CheckpointTests(unittest.TestCase):
    def test_reopen_preserves_only_confirmed_parts(self):
        from modules.upos_resume import UploadCheckpoint
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / 'sample'; media.write_bytes(b'abcdef')
            root = Path(tmp) / 'private'
            with UploadCheckpoint(root, media, 'account-digest') as cp:
                cp.set_session({'chunk_size': 2, 'auth': 'PRIVATE', 'upload_id': 'PRIVATE'})
                cp.confirm(0)
                cp.confirm(2)
            with UploadCheckpoint(root, media, 'account-digest') as cp:
                self.assertEqual(cp.confirmed, {0, 2})
                self.assertEqual(cp.session['chunk_size'], 2)
                self.assertEqual(cp.phase, 'uploading')
            import os
            if os.name != 'nt':  # Windows chmod exposes only its read-only flag.
                self.assertEqual(root.stat().st_mode & 0o777, 0o700)
                self.assertEqual(next(root.glob('*.json')).stat().st_mode & 0o777, 0o600)

    def test_upload_page_restarts_and_skips_confirmed_parts(self):
        import asyncio, types
        from test_bili_chunk_retry_policy import U
        from modules.upos_resume import UploadCheckpoint
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                f=Path(tmp)/'media'; f.write_bytes(b'abcdef')
                page=types.SimpleNamespace(path=str(f),get_size=lambda:6)
                calls=[]; pre_calls=[]; fail=[True]
                async def pre(p):
                    pre_calls.append(1); return dict(chunk_size=2,threads=1,upload_id='PRIVATE')
                async def chunk(p,offset,number,*args):
                    calls.append(number)
                    if number==1 and fail[0]: raise ConnectionError('interruption')
                async def complete(*a):return {'filename':'merged','cid':1}
                for interrupted in [True,False]:
                    with UploadCheckpoint(Path(tmp)/'private',f,'account') as cp:
                        u=U(); u.resume=cp; u._preupload=pre;u._upload_chunk=chunk;u._complete_page=complete
                        if interrupted:
                            with self.assertRaises(ConnectionError):await u._upload_page(page)
                        else:self.assertEqual(await u._upload_page(page),{'filename':'merged','cid':1})
                    fail[0]=False
                self.assertEqual(pre_calls,[1])
                self.assertEqual(calls,[0,1,1,2])
        asyncio.run(run())

    def test_unknown_post_blocks_retry_and_confirmed_merge_drops_auth(self):
        from modules.upos_resume import UploadCheckpoint
        with tempfile.TemporaryDirectory() as tmp:
            f=Path(tmp)/'media'; f.write_bytes(b'x'); root=Path(tmp)/'private'
            with UploadCheckpoint(root,f,'account') as cp:
                cp.set_session({'auth':'PRIVATE'})
                cp.mark('merging')
            with self.assertRaisesRegex(ValueError,'未知'):
                with UploadCheckpoint(root,f,'account'): pass
            # A separate media identity exercises confirmed completion cleanup.
            g=Path(tmp)/'other';g.write_bytes(b'y')
            with UploadCheckpoint(root,g,'account') as cp:
                cp.set_session({'auth':'PRIVATE'});cp.complete({'filename':'f','cid':1})
                self.assertIsNone(cp.session)
                self.assertNotIn('PRIVATE',cp.path.read_text())
                cp.clear();self.assertFalse(cp.path.exists())

    def test_identity_mismatch_and_duplicate_click_fail_closed(self):
        from modules.upos_resume import UploadCheckpoint
        with tempfile.TemporaryDirectory() as tmp:
            f=Path(tmp)/'media';f.write_bytes(b'abc');root=Path(tmp)/'private'
            with UploadCheckpoint(root,f,'account') as cp:
                cp.set_session({'auth':'PRIVATE'})
                with self.assertRaisesRegex(ValueError,'正在进行'):
                    with UploadCheckpoint(root,f,'account'):pass
            for account,content in [('other',b'abc'),('account',b'xyz')]:
                if content!=b'abc':f.write_bytes(content)
                with self.assertRaisesRegex(ValueError,'不匹配'):
                    with UploadCheckpoint(root,f,account):pass

    def test_failed_atomic_replace_preserves_previous_record(self):
        import json
        from unittest.mock import patch
        from modules.upos_resume import UploadCheckpoint
        with tempfile.TemporaryDirectory() as tmp:
            f=Path(tmp)/'media';f.write_bytes(b'abc');root=Path(tmp)/'private'
            with UploadCheckpoint(root,f,'account') as cp:
                cp.set_session({'auth':'PRIVATE'});cp.confirm(0)
                with patch('modules.upos_resume.os.replace',side_effect=OSError('power loss')):
                    with self.assertRaises(OSError):cp.confirm(1)
                self.assertEqual(json.loads(cp.path.read_text())['confirmed'],[0])
                self.assertFalse(list(root.glob('.upos-*')))

    def test_entry_holds_lock_and_blocks_repeating_unknown_submit(self):
        import ast, asyncio
        from modules.upos_resume import UploadCheckpoint
        source=(Path(__file__).resolve().parents[1]/'modules/bilibili_uploader.py').read_text()
        node=next((n for n in ast.parse(source).body if isinstance(n,ast.AsyncFunctionDef) and n.name=='_run_resumable_upload'),None)
        self.assertIsNotNone(node,'normal upload entry must enable checkpoints')
        env={'UploadCheckpoint':UploadCheckpoint}
        async def start(u,cancel):return await u.start()
        env['_start_cancellable_upload']=start
        exec(compile(ast.Module(body=[node],type_ignores=[]),'<entry>','exec'),env)
        with tempfile.TemporaryDirectory() as tmp:
            f=Path(tmp)/'media';f.write_bytes(b'x');root=Path(tmp)/'private'
            class Upload:
                pages=[type('Page',(),{'path':str(f)})()]
                async def start(self):
                    self.resume.mark('submitting')
                    raise TimeoutError()
            with self.assertRaises(TimeoutError):asyncio.run(env['_run_resumable_upload'](Upload(),root,'account',None))
            with self.assertRaisesRegex(ValueError,'未知'):asyncio.run(env['_run_resumable_upload'](Upload(),root,'account',None))

    def test_confirmed_submission_is_returned_without_second_start(self):
        import ast, asyncio, threading
        from modules.upos_resume import UploadCheckpoint
        source=(Path(__file__).resolve().parents[1]/'modules/bilibili_uploader.py').read_text(encoding='utf8')
        node=next(n for n in ast.parse(source).body if isinstance(n,ast.AsyncFunctionDef) and n.name=='_run_resumable_upload')
        calls=[]
        async def start(u, flag):
            calls.append(1)
            self.assertTrue(flag._upos_recovery_active)
            u.resume.mark('submitting')
            return {'data': {'bvid': 'BV-fixture', 'aid': 1}}
        env={'UploadCheckpoint':UploadCheckpoint,'_start_cancellable_upload':start}
        exec(compile(ast.Module(body=[node],type_ignores=[]),'<entry>','exec'),env)
        with tempfile.TemporaryDirectory() as tmp:
            media=Path(tmp)/'media';media.write_bytes(b'x')
            uploader=type('Uploader',(),{'pages':[type('Page',(),{'path':str(media)})()]})()
            flag=threading.Event()
            first=asyncio.run(env['_run_resumable_upload'](uploader,Path(tmp)/'private','account',flag))
            second=asyncio.run(env['_run_resumable_upload'](uploader,Path(tmp)/'private','account',flag))
            self.assertEqual(first,second)
            self.assertEqual(calls,[1])
            self.assertFalse(flag._upos_recovery_active)

    def test_new_private_directory_parent_is_synced(self):
        import os
        from unittest.mock import patch
        from modules.upos_resume import UploadCheckpoint
        if os.name=='nt':self.skipTest('POSIX durability')
        with tempfile.TemporaryDirectory() as tmp:
            f=Path(tmp)/'media';f.write_bytes(b'x');root=Path(tmp)/'private'
            opened=[]; real_open=os.open
            def record(path,*a,**kw):
                opened.append(str(path));return real_open(path,*a,**kw)
            with patch('modules.upos_resume.os.open',side_effect=record):
                with UploadCheckpoint(root,f,'account') as cp:cp.set_session({'auth':'PRIVATE'})
            self.assertIn(str(root.parent),opened)

    def test_explicit_submit_rejection_restores_confirmed_merge(self):
        import ast, asyncio
        from modules.bili_sdk.exceptions.ResponseCodeException import ResponseCodeException
        text=(Path(__file__).resolve().parents[1]/'modules/bili_sdk/video_uploader.py').read_text()
        cl=next(n for n in ast.parse(text).body if isinstance(n,ast.ClassDef) and n.name=='VideoUploader')
        node=next(n for n in cl.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='_main')
        env={'ResponseCodeException':ResponseCodeException}
        exec(compile(ast.Module(body=[node],type_ignores=[]),'<main>','exec'),env)
        class Resume:
            phase='complete'
            def mark(self,v):self.phase=v
        class U:
            resume=Resume(); pages=[]
            async def _upload_cover(self):return 'cover'
            async def _submit(self,*a):raise ResponseCodeException(-400,'rejected',{})
        u=U()
        with self.assertRaises(ResponseCodeException):asyncio.run(env['_main'](u))
        self.assertEqual(u.resume.phase,'complete')

    def test_concurrent_parts_cancel_and_resume_consistently(self):
        import asyncio, types
        from test_bili_chunk_retry_policy import U
        from modules.upos_resume import UploadCheckpoint
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                f=Path(tmp)/'media';f.write_bytes(b'abc');root=Path(tmp)/'private'
                page=types.SimpleNamespace(path=str(f),get_size=lambda:3)
                with UploadCheckpoint(root,f,'account') as cp:
                    u=U();u.resume=cp
                    async def pre(p):return dict(chunk_size=1,threads=3,upload_id='private')
                    ready=asyncio.Event()
                    async def chunk(p,o,n,*a):
                        if n in (0,2):return
                        ready.set();await asyncio.sleep(10)
                    u._preupload=pre;u._upload_chunk=chunk
                    task=asyncio.create_task(u._upload_page(page));await ready.wait();await asyncio.sleep(.01)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):await task
                with UploadCheckpoint(root,f,'account') as cp:self.assertEqual(cp.confirmed,{0,2})
        asyncio.run(run())

    def test_cross_process_lock_prevents_duplicate_upload(self):
        import os, subprocess, sys
        from modules.upos_resume import UploadCheckpoint
        with tempfile.TemporaryDirectory() as tmp:
            f=Path(tmp)/'media';f.write_bytes(b'a');root=Path(tmp)/'private'
            script='from modules.upos_resume import UploadCheckpoint; import sys;\ntry:\n with UploadCheckpoint(sys.argv[1],sys.argv[2],"account"):pass\nexcept (OSError,ValueError):sys.exit(19)'
            env=dict(os.environ,PYTHONPATH=str(Path(__file__).resolve().parents[1]))
            with UploadCheckpoint(root,f,'account'):
                child=subprocess.run([sys.executable,'-c',script,str(root),str(f)],env=env,timeout=10)
                self.assertEqual(child.returncode,19)


if __name__ == '__main__': unittest.main()
