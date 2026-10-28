import asyncio
import hashlib
import hmac
import json
from pathlib import Path
import tempfile
import time
import unittest
from urllib.parse import urlencode
from aiohttp.test_utils import TestClient, TestServer
from core import Store, Scheduler
from demo import DemoCalendar
from web_api import create_app
from web_auth import AuthError, validate_init_data

TOKEN='12345:test-only-not-a-real-token'
def signed(uid=42,stamp=None,**fields):
    values={'auth_date':str(int(time.time()) if stamp is None else stamp),'user':json.dumps({'id':uid,'first_name':'Test'},separators=(',',':')),**fields}
    secret=hmac.new(b'WebAppData',TOKEN.encode(),hashlib.sha256).digest()
    values['hash']=hmac.new(secret,'\n'.join(f'{k}={values[k]}' for k in sorted(values)).encode(),hashlib.sha256).hexdigest()
    return urlencode(values)

class AuthTests(unittest.TestCase):
    def test_valid_signature_and_64bit_id(self):
        self.assertEqual(validate_init_data(signed(12345678901),TOKEN)['id'],12345678901)
    def test_tampering_expiry_future_and_duplicate_fields(self):
        valid=signed()
        for value in [valid.replace('Test','Other'),signed(stamp=int(time.time())-3601),signed(stamp=int(time.time())+60),valid+'&auth_date=1','',signed(uid=True)]:
            with self.subTest(value=value[:10]):
                with self.assertRaises(AuthError):validate_init_data(value,TOKEN)

class WebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'db.sqlite3'
        self.s=Store(str(self.path));self.s.set('admin_id',1)
        self.s.profile(42,name='Test User',email='test@example.com')
        self.cal=DemoCalendar();self.scheduler=Scheduler(self.s,self.cal)
        self.client=TestClient(TestServer(create_app(self.s,self.scheduler,asyncio.Lock(),TOKEN)))
        await self.client.start_server()
    async def asyncTearDown(self):
        await self.client.close();self.s.db.close();self.tmp.cleanup()
    def headers(self,uid=42,key=None):
        out={'X-Telegram-Init-Data':signed(uid)}
        if key:out['Idempotency-Key']=key
        return out
    async def draft(self):
        from datetime import datetime,timedelta
        from zoneinfo import ZoneInfo
        today=datetime.now(ZoneInfo('Asia/Kamchatka')).date()
        for i in range(2,10):
            slots=await self.scheduler.available(today+timedelta(days=i),30)
            if slots:return {'title':'A meeting','name':'Test User','email':'test@example.com','description':'','duration':30,'start':int(slots[0])}
        self.fail('No demo slots')
    async def test_auth_and_demo_bypass_disabled(self):
        for path in ['/meet/api/bootstrap','/meet/api/requests?user_id=1&admin=1']:
            r=await self.client.get(path,headers={'Cookie':'demo_role=admin'})
            self.assertEqual(r.status,401)
        self.assertEqual((await self.client.get('/meet/demo/admin')).status,404)
        self.assertEqual((await self.client.get('/meet/.env')).status,404)
    async def test_only_admin_can_change_settings(self):
        r=await self.client.post('/meet/api/admin/settings',json={'daily_limit':2},headers=self.headers())
        self.assertNotEqual(r.status,200)
        self.assertEqual(self.s.settings()['daily_limit'],4)
        r=await self.client.post('/meet/api/admin/settings',json={'start':'19:00','end':'18:00'},headers=self.headers(1))
        self.assertEqual(r.status,400)
        r=await self.client.post('/meet/api/admin/settings',json={'daily_limit':3},headers=self.headers(1))
        self.assertEqual(r.status,200);self.assertEqual(self.s.settings()['daily_limit'],3)
    async def test_idempotency_and_slot_race_survive_reopen(self):
        draft=await self.draft();key='test-key-1234567890'
        async def send(k):return await self.client.post('/meet/api/requests',json=draft,headers=self.headers(key=k))
        one,two=await asyncio.gather(send(key),send(key))
        self.assertEqual({one.status,two.status},{200,201})
        r=(await one.json())['request'];self.assertEqual(r['id'],(await two.json())['request']['id'])
        self.assertEqual((await send('different-key-1234567')).status,409)
        self.s.db.close();self.s.db=__import__('sqlite3').connect(str(self.path));self.s.db.row_factory=__import__('sqlite3').Row
        self.assertEqual((await send(key)).status,200)
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM requests').fetchone()[0],1)
    async def test_cannot_read_change_or_move_other_users_request(self):
        draft=await self.draft();r=await self.scheduler.submit(42,draft)
        response=await self.client.get('/meet/api/requests',headers=self.headers(43))
        self.assertEqual((await response.json())['items'],[])
        response=await self.client.post(f'/meet/api/requests/{r["id"]}/action',json={'action':'cancel'},headers=self.headers(43))
        self.assertEqual(response.status,404)
        self.assertEqual(self.s.row(r['id'])['status'],'pending')
    async def test_calendar_missing_fails_closed(self):
        self.cal.configured=lambda:False
        r=await self.client.get('/meet/api/slots?date=2026-11-10&duration=30',headers=self.headers())
        self.assertEqual(r.status,503)
        r=await self.client.post('/meet/api/requests',json=await self.draft(),headers=self.headers(key='missing-cal-key-123'))
        self.assertEqual(r.status,503)
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM requests').fetchone()[0],0)
    async def test_html_text_and_profile_validation(self):
        r=await self.client.post('/meet/api/profile',json={'name':'Test','email':'not-email'},headers=self.headers())
        self.assertEqual(r.status,400)
        r=await self.client.get('/meet/',headers=self.headers())
        self.assertEqual(r.status,200)
        self.assertIn("script-src 'self' https://telegram.org",r.headers['Content-Security-Policy'])
    async def test_exception_add_remove_and_blocking(self):
        b={'date':'2026-12-01','start':'10:00','end':'11:00'}
        r=await self.client.post('/meet/api/admin/exceptions',json=b,headers=self.headers(1));self.assertEqual(r.status,200)
        self.assertIn(b,self.s.settings()['closed'])
        r=await self.client.delete('/meet/api/admin/exceptions',json=b,headers=self.headers(1));self.assertEqual(r.status,200)
        self.assertNotIn(b,self.s.settings()['closed'])
    async def test_foreign_origin_rejected(self):
        r=await self.client.post('/meet/api/profile',json={'name':'X','email':'x@example.com'},headers={**self.headers(),'Origin':'https://other.example'})
        self.assertEqual(r.status,403)


    async def test_approve_then_cancel_real_http_routes(self):
        r=await self.scheduler.submit(42,await self.draft())
        response=await self.client.post(f'/meet/api/requests/{r["id"]}/action',json={'action':'approve'},headers=self.headers(1))
        self.assertEqual(response.status,200)
        self.assertEqual((await response.json())['request']['status'],'approved')
        response=await self.client.post(f'/meet/api/requests/{r["id"]}/action',json={'action':'cancel'},headers=self.headers())
        self.assertEqual(response.status,200)
        self.assertEqual(self.s.row(r['id'])['status'],'cancelled')
    async def test_ban_rejects_pending_and_unban_keeps_history(self):
        r=await self.scheduler.submit(42,await self.draft())
        response=await self.client.post(f'/meet/api/requests/{r["id"]}/action',json={'action':'ban'},headers=self.headers(1))
        self.assertEqual(response.status,200)
        self.assertEqual(self.s.row(r['id'])['status'],'rejected')
        self.assertEqual(self.s.user(42)['banned'],1)
        response=await self.client.post('/meet/api/admin/blacklist',json={'user_id':42},headers=self.headers(1))
        self.assertEqual(response.status,200)
        self.assertEqual(self.s.user(42)['banned'],0)
        self.assertEqual(self.s.row(r['id'])['status'],'rejected')
