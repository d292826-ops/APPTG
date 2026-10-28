import asyncio
import tempfile
import time
import unittest
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
from core import Store,Scheduler,RuleError

class Calendar:
    def __init__(self): self.events={}; self.fail=False; self.created=0
    async def busy(self,a,b): return [(r['start'],r['end'],k) for k,r in self.events.items()]
    async def upsert(self,r,parent=None):
        eid=parent['event_id'] if parent else r['event_id']
        if eid not in self.events: self.created+=1
        self.events[eid]=dict(r)
        if self.fail: raise RuntimeError('simulated lost response after creation')
        return {'id':eid,'hangoutLink':'https://meet.google.com/test'}
    async def delete(self,eid):
        if self.fail: raise RuntimeError('offline')
        self.events.pop(eid,None)

class BookingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.s=Store(self.tmp.name+'/db'); self.s.set('admin_id',1)
        self.s.profile(2,name='Guest',username='guest',email='a@example.com')
        settings=self.s.settings(); settings.update(weekdays=list(range(7)),min_hours=0,timezone='Asia/Kamchatka')
        self.s.set('settings',settings); self.cal=Calendar(); self.q=Scheduler(self.s,self.cal)
        self.day=(datetime.now(ZoneInfo(settings['timezone']))+timedelta(days=2)).date()
    async def asyncTearDown(self): self.s.db.close(); self.tmp.cleanup()
    async def draft(self,shift=0,parent=None):
        slots=await self.q.available(self.day+timedelta(days=shift),30,parent=parent)
        return dict(start=slots[0],duration=30,title='Call',description='Agenda',name='Guest',email='a@example.com',parent=parent)
    async def test_reservation_and_race(self):
        d=await self.draft()
        results=await asyncio.gather(self.q.submit(2,d),self.q.submit(2,d),return_exceptions=True)
        self.assertEqual(sum(isinstance(x,RuleError) for x in results),1)
        slots=await self.q.available(self.day,30); self.assertNotIn(d['start'],slots)
    async def test_reject_releases(self):
        d=await self.draft(); r=await self.q.submit(2,d); await self.q.reject(r['id'],1,'Busy')
        self.assertIn(d['start'],await self.q.available(self.day,30)); self.assertEqual(self.s.row(r['id'])['comment'],'Busy')
    async def test_expiry_and_profile_survives_cleanup(self):
        r=await self.q.submit(2,await self.draft()); self.s.update(r['id'],deadline=time.time()-1); self.s.cleanup()
        self.assertEqual(self.s.row(r['id'])['status'],'expired')
        self.s.update(r['id'],closed=time.time()-31*86400); self.s.cleanup()
        with self.assertRaises(RuleError): self.s.row(r['id'])
        self.assertEqual(self.s.user(2)['email'],'a@example.com')
    async def test_permissions(self):
        r=await self.q.submit(2,await self.draft())
        with self.assertRaises(RuleError): await self.q.approve(r['id'],2)
        with self.assertRaises(RuleError): self.s.owned(r['id'],3)
        with self.assertRaises(RuleError): await self.q.cancel(r['id'],3)
    async def test_double_approval_single_event(self):
        r=await self.q.submit(2,await self.draft()); await self.q.approve(r['id'],1); await self.q.approve(r['id'],1)
        self.assertEqual(self.cal.created,1); self.assertEqual(len(self.cal.events),1)
    async def test_uncertain_creation_recovers(self):
        r=await self.q.submit(2,await self.draft()); self.cal.fail=True
        with self.assertRaises(RuntimeError): await self.q.approve(r['id'],1)
        self.assertEqual(self.s.row(r['id'])['status'],'approving')
        self.cal.fail=False; await self.q.approve(r['id'],1)
        self.assertEqual(self.cal.created,1); self.assertEqual(self.s.row(r['id'])['status'],'approved')
    async def test_conflict_after_submission(self):
        d=await self.draft(); r=await self.q.submit(2,d); self.cal.events['external']={'start':d['start'],'end':d['start']+3600}
        with self.assertRaises(RuleError): await self.q.approve(r['id'],1)
        self.assertEqual(self.s.row(r['id'])['status'],'conflict'); self.assertEqual(self.cal.created,0)
    async def test_move_keeps_old_until_approval(self):
        r=await self.q.submit(2,await self.draft()); await self.q.approve(r['id'],1)
        move=await self.q.submit(2,await self.draft(1,r['id']))
        self.assertEqual(self.s.row(r['id'])['status'],'approved'); self.assertEqual(len(self.cal.events),1)
        await self.q.approve(move['id'],1)
        self.assertEqual(self.s.row(r['id'])['status'],'moved'); self.assertEqual(len(self.cal.events),1)
        self.assertEqual(self.s.row(r['id'])['event_id'],self.s.row(move['id'])['event_id'])
    async def test_failed_move_rejection_preserves_old(self):
        r=await self.q.submit(2,await self.draft()); await self.q.approve(r['id'],1)
        move=await self.q.submit(2,await self.draft(1,r['id'])); await self.q.reject(move['id'],1)
        self.assertEqual(self.s.row(r['id'])['status'],'approved'); self.assertEqual(len(self.cal.events),1)
    async def test_cancel_failure_does_not_claim_success(self):
        r=await self.q.submit(2,await self.draft()); await self.q.approve(r['id'],1); self.cal.fail=True
        with self.assertRaises(RuntimeError): await self.q.cancel(r['id'],2)
        self.assertEqual(self.s.row(r['id'])['status'],'cancelling')
        self.cal.fail=False; await self.q.cancel(r['id'],2)
        self.assertEqual(self.s.row(r['id'])['status'],'cancelled'); self.assertEqual(len(self.cal.events),0)
    async def test_ban_rejects_pending_not_approved(self):
        r=await self.q.submit(2,await self.draft()); await self.q.approve(r['id'],1)
        p=await self.q.submit(2,await self.draft(1)); await self.q.ban(2,1)
        self.assertEqual(self.s.row(p['id'])['status'],'rejected'); self.assertEqual(self.s.row(r['id'])['status'],'approved')
        with self.assertRaises(RuleError): await self.q.submit(2,await self.draft(2))
        await self.q.ban(2,1,False); self.assertFalse(self.s.user(2)['banned'])
    async def test_all_durations_and_grid(self):
        for duration in (15,30,45,60,90):
            slots=await self.q.available(self.day,duration); self.assertTrue(slots)
            self.assertTrue(all(datetime.fromtimestamp(x,ZoneInfo('Asia/Kamchatka')).minute in (0,30) for x in slots))
    async def test_restrictions_and_daily_limit(self):
        settings=self.s.settings(); settings['closed']=[{'date':str(self.day)}]; self.s.set('settings',settings)
        self.assertEqual(await self.q.available(self.day,30),[])
        settings['closed']=[]; settings['daily_limit']=1; self.s.set('settings',settings)
        await self.q.submit(2,await self.draft()); self.assertEqual(await self.q.available(self.day,30),[])
    async def test_approval_does_not_reapply_booking_lead_time(self):
        settings=self.s.settings(); settings['min_hours']=24; self.s.set('settings',settings)
        r=await self.q.submit(2,await self.draft())
        # Existing valid booking can be approved after booking cutoff has moved forward.
        settings['min_hours']=200; self.s.set('settings',settings)
        await self.q.approve(r['id'],1); self.assertEqual(self.s.row(r['id'])['status'],'approved')
    async def test_after_buffer_protects_previous_event(self):
        settings=self.s.settings(); settings['buffer_after']=30; self.s.set('settings',settings)
        tz=ZoneInfo(settings['timezone'])
        a=datetime.combine(self.day,datetime.min.time(),tzinfo=tz).timestamp()+10*3600
        self.cal.events['external']={'start':a,'end':a+3600}
        slots=await self.q.available(self.day,30)
        self.assertNotIn(a+3600,slots)
        self.assertIn(a+5400,slots)

    async def test_persistence_after_reopen(self):
        r=await self.q.submit(2,await self.draft()); path=self.tmp.name+'/db'; self.s.db.close(); self.s=Store(path)
        self.assertEqual(self.s.row(r['id'])['status'],'pending'); self.assertEqual(self.s.get('admin_id'),1)

if __name__=='__main__': unittest.main()
