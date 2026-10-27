"""Ежедневная уборка старых данных: черновики, outbox, audit, pending_inputs.

Брони и платежи уборка не трогает — это проверяется отдельным тестом.
"""
import os
import secrets
import unittest
from dataclasses import replace
from datetime import timedelta

from booking_store import BookingStore
from settings import load

URL = os.getenv('TEST_DATABASE_URL', '')


@unittest.skipUnless(URL, 'TEST_DATABASE_URL is required for PostgreSQL tests')
class Cleanup(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = 'test_cleanup_' + secrets.token_hex(8)
        self.settings = replace(load(), database_url=URL, admin_ids=(1,),
                                web_url='https://core.test')
        self.store = await BookingStore.connect(self.settings, self.schema)

    async def asyncTearDown(self):
        await self.store.pool.execute(f'DROP SCHEMA {self.schema} CASCADE')
        await self.store.pool.close()

    async def draft(self, actor, age_days, state='name'):
        await self.store.pool.execute(
            'INSERT INTO drafts(telegram_id,token,state,data,revision,updated_at) '
            "VALUES($1,$2,$3,'{}'::jsonb,0,now()-$4*interval '1 day')",
            actor, f'token-{actor}-{age_days}', state, float(age_days))

    async def outbox(self, ident, age_days, delivered=True, tag=''):
        await self.store.pool.execute(
            'INSERT INTO outbox(booking_id,recipient,event,text,delivered_at) '
            "VALUES($1,1,$2,'text',CASE WHEN $3::boolean THEN now()-$4*interval '1 day' ELSE NULL END)",
            ident, f'event-{ident}-{age_days}{tag}', delivered, float(age_days))

    async def audit(self, ident, age_days):
        await self.store.pool.execute(
            "INSERT INTO audit(booking_id,actor,action,detail,created_at) "
            "VALUES($1,1,'test','{}'::jsonb,now()-$2*interval '1 day')", ident, float(age_days))

    async def pending(self, actor, minutes):
        """minutes<0 — срок вышел, minutes>0 — ещё ждём ответа гостя."""
        await self.store.pool.execute(
            'INSERT INTO pending_inputs(actor,kind,ref,expires_at) '
            "VALUES($1,'phone','b1',now()+$2*interval '1 minute')", actor, float(minutes))

    async def booking(self, ident='B1'):
        await self.store.pool.execute(
            'INSERT INTO bookings(id,draft_token,telegram_id,starts_at,ends_at,blocked_until,'
            "data,estimate,rules_hash,created_at,status,paid) "
            "VALUES($1,'tok',1,now()+interval '1 day',now()+interval '1 day',now()+interval '1 day',"
            "'{}'::jsonb,'{}'::jsonb,'h',now(),'confirmed',0)", ident)
        return ident

    async def count(self, table, where='', args=()):
        sql = f'SELECT count(*) FROM {table} {where}'
        return await self.store.pool.fetchval(sql, *args)

    async def test_old_drafts_go_fresh_stay(self):
        await self.draft(10, 3)
        await self.draft(11, 1)
        counts = await self.store.cleanup_old_data()
        self.assertEqual(counts['drafts'], 1)
        self.assertEqual(await self.count('drafts', 'WHERE telegram_id=10'), 0)
        self.assertEqual(await self.count('drafts', 'WHERE telegram_id=11'), 1)

    async def test_delivered_outbox_go_undelivered_stays(self):
        ident = await self.booking()
        await self.outbox(ident, 40, delivered=True, tag='-a')
        await self.outbox(ident, 40, delivered=False, tag='-b')
        await self.outbox(ident, 10, delivered=True, tag='-c')
        counts = await self.store.cleanup_old_data()
        self.assertEqual(counts['outbox'], 1)
        self.assertEqual(await self.count('outbox', 'WHERE delivered_at IS NOT NULL'), 1)
        self.assertEqual(await self.count('outbox', 'WHERE delivered_at IS NULL'), 1)

    async def test_old_audit_goes_recent_stays(self):
        ident = await self.booking()
        await self.audit(ident, 400)
        await self.audit(ident, 10)
        counts = await self.store.cleanup_old_data()
        self.assertEqual(counts['audit'], 1)
        self.assertEqual(await self.count('audit'), 1)

    async def test_expired_pending_input_goes_valid_stays(self):
        await self.pending(20, -60)
        await self.pending(21, 60)
        counts = await self.store.cleanup_old_data()
        self.assertEqual(counts['pending_inputs'], 1)
        self.assertEqual(await self.count('pending_inputs', 'WHERE actor=20'), 0)
        self.assertEqual(await self.count('pending_inputs', 'WHERE actor=21'), 1)

    async def test_bookings_and_payments_survive(self):
        ident = await self.booking()
        await self.store.pool.execute(
            'INSERT INTO payment_requests(id,booking_id,kind,amount,paid_before,state) '
            "VALUES('pr1',$1,'deposit',1000,0,'requested')", ident)
        await self.draft(30, 5)
        await self.outbox(ident, 40, delivered=True, tag='-x')
        await self.audit(ident, 400)
        await self.pending(30, -60)
        await self.store.cleanup_old_data()
        self.assertEqual(await self.count('bookings'), 1)
        self.assertEqual(await self.count('payment_requests'), 1)
        self.assertEqual(await self.count('bookings', 'WHERE id=$1', (ident,)), 1)

    async def test_cleanup_is_idempotent(self):
        await self.draft(40, 5)
        await self.store.cleanup_old_data()
        counts = await self.store.cleanup_old_data()
        self.assertEqual(counts['drafts'], 0)


if __name__ == '__main__':
    unittest.main()
