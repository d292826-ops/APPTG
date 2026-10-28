import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
import httpx
from google_calendar import GoogleCalendar,CalendarError

class GoogleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.c=GoogleCalendar(); self.c.access=AsyncMock(return_value='test-access')
        self.r=dict(id=3,event_id='a'*32,title='Call',description='Agenda',name='Guest',username='guest',email='guest@example.com',start=1800000000,end=1800001800)
    async def asyncTearDown(self): await self.c.close()
    async def test_insert_parameters(self):
        self.c.client.get=AsyncMock(return_value=httpx.Response(404))
        self.c.request=AsyncMock(return_value={'id':'a'*32,'hangoutLink':'https://meet.google.com/test'})
        result=await self.c.upsert(self.r)
        args,kw=self.c.request.call_args
        self.assertEqual(args,('POST','/events'))
        self.assertEqual(kw['params'],{'sendUpdates':'all','conferenceDataVersion':1})
        self.assertEqual(kw['json']['id'],'a'*32)
        self.assertEqual(kw['json']['attendees'],[{'email':'guest@example.com'}])
        self.assertEqual(kw['json']['reminders']['overrides'][0]['minutes'],60)
    async def test_existing_event_not_inserted_again(self):
        self.c.client.get=AsyncMock(return_value=httpx.Response(200,json={'id':'a'*32,'hangoutLink':'https://meet.google.com/test'}))
        self.c.request=AsyncMock()
        await self.c.upsert(self.r); self.c.request.assert_not_awaited()
    async def test_move_patches_original(self):
        self.c.request=AsyncMock(return_value={'id':'old','hangoutLink':'https://meet.google.com/test'})
        await self.c.upsert(self.r,{'event_id':'old'})
        args,kw=self.c.request.call_args; self.assertEqual(args,('PATCH','/events/old')); self.assertNotIn('conferenceData',kw['json'])
    async def test_busy_filters_transparent_and_cancelled(self):
        self.c.request=AsyncMock(return_value=dict(timeZone='Asia/Kamchatka',items=[
            dict(id='all',start={'date':'2026-11-01'},end={'date':'2026-11-02'}),
            dict(id='free',transparency='transparent'),dict(id='cancel',status='cancelled')]))
        rows=await self.c.busy(0,1); self.assertEqual(len(rows),1); self.assertEqual(rows[0][1]-rows[0][0],86400)
    async def test_unauthorized_is_error(self):
        self.c.client.request=AsyncMock(return_value=httpx.Response(401))
        with self.assertRaises(CalendarError): await self.c.request('GET','/events')
    async def test_delete_idempotent(self):
        self.c.client.delete=AsyncMock(return_value=httpx.Response(410)); await self.c.delete('old')
        self.c.client.delete=AsyncMock(return_value=httpx.Response(503))
        with self.assertRaises(CalendarError): await self.c.delete('old')
