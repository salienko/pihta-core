"""Чек из ОФД: только ссылка, конечное число попыток и ручная перепроверка.

Проверяем требования владельца:
1. чек найден → у платежа formed и ссылка на чек;
2. чек не найден → после лимита попыток missing, автоматические опросы прекращаются;
3. ошибка ОФД → failed, тоже без бесконечных опросов;
4. ручная перепроверка из админки снова спрашивает ОФД;
5. в админке видно «чек не сформирован», а не «Чек формируется»;
6. чек ищется и для неопознанных платежей.
"""
import hashlib
import os
import secrets
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp.test_utils import TestClient, TestServer

from settings import load
from booking_store import BookingStore
from booking_domain import TZ, estimate, RuleError
from paykeeper import ProviderError
from paykeeper_store import RECEIPT_ATTEMPTS
from admin_web import make_app

URL = os.getenv('TEST_DATABASE_URL', '')
PHONE = '+79001234567'
PAYMENT = '4540'
RECEIPT = dict(id='1725', url='https://paykeeper.example/receipt/fop-key-1725/?rc_id=1725')


class Setup(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = 'test_receipts_' + secrets.token_hex(6)
        self.settings = replace(load(), database_url=URL, admin_ids=(1,), web_url='https://pihta.test',
                                paykeeper_enabled=True, paykeeper_url='https://paykeeper.example',
                                paykeeper_secret='synthetic-secret', paykeeper_tax='none',
                                paykeeper_deposit_type='part_prepay', paykeeper_full_type='prepay',
                                paykeeper_balance_type='part_prepay', paykeeper_settlement_enabled=False)
        self.store = await BookingStore.connect(self.settings, self.schema)
        self.provider = SimpleNamespace(
            invoice=AsyncMock(return_value=('1001', 'https://paykeeper.example/bill/1001/')),
            invoice_status=AsyncMock(return_value={'status': 'paid', 'paymentid': PAYMENT}),
            receipts=AsyncMock(return_value=[]))
        self.store.provider = lambda: self.provider
        start = (datetime.now(TZ) + timedelta(days=4)).replace(hour=13, minute=0, second=0, microsecond=0)
        token = await self.store.begin(2)
        d = await self.store.draft(2)
        catalog, holidays, rates = await self.store.pricing()
        data = dict(d['data'], name='Гость', phone=PHONE, day=start.date().isoformat(),
                    start=start.isoformat(), consent=True, rules_hash=self.settings.rules_hash)
        data['quote'] = estimate(data, catalog, holidays)
        await self.store.save(2, d, 'review', data)
        self.ident = (await self.store.submit(2, token))[0]
        await self.store.pool.execute('UPDATE outbox SET delivered_at=now()')
        self.req, _ = await self.store.request_payment(2, self.ident, 'deposit')
        await self.store.invoice_once()
        await self.store.accept_paykeeper(dict(id=PAYMENT, orderid=self.req['id']), 200000)
        self.payment_id = PAYMENT

    async def asyncTearDown(self):
        await self.store.pool.execute(f'DROP SCHEMA {self.schema} CASCADE')
        await self.store.pool.close()

    async def payment(self):
        return await self.store.pool.fetchrow('SELECT * FROM paykeeper_payments WHERE payment_id=$1', self.payment_id)

    async def drain(self, limit=RECEIPT_ATTEMPTS + 5):
        """Крутим опрос, каждый раз сдвигая срок: ждём терминального состояния чека."""
        calls = 0
        while calls < limit:
            if await self.store.pool.fetchval(
                    'SELECT receipt_state FROM paykeeper_payments WHERE payment_id=$1', self.payment_id) != 'waiting':
                break
            await self.store.pool.execute(
                'UPDATE paykeeper_payments SET next_receipt_at=now() WHERE payment_id=$1', self.payment_id)
            await self.store.receipt_once()
            calls += 1
        return calls


@unittest.skipUnless(URL, 'TEST_DATABASE_URL required')
class ReceiptLink(Setup):
    async def test_found_receipt_stores_link_and_state(self):
        self.provider.receipts.return_value = [RECEIPT]
        self.assertTrue(await self.store.receipt_once())
        p = await self.payment()
        self.assertEqual(p['receipt_state'], 'formed')
        self.assertTrue(p['receipt_done'])
        url = await self.store.pool.fetchval('SELECT url FROM paykeeper_fiscal_receipts WHERE payment_id=$1',
                                             self.payment_id)
        self.assertEqual(url, RECEIPT['url'])
        self.assertEqual(await self.store.receipt_delivery_url('pkreceipt:1725'), RECEIPT['url'])
        # админу сообщение о готовом чеке уходит всегда
        self.assertTrue(await self.store.pool.fetchval(
            "SELECT EXISTS(SELECT 1 FROM outbox WHERE event='pkreceiptadmin:1725')"))

    async def test_missing_after_limit_stops_polling(self):
        calls = await self.drain()
        self.assertEqual(calls, RECEIPT_ATTEMPTS)
        p = await self.payment()
        self.assertEqual(p['receipt_state'], 'missing')
        self.assertFalse(p['receipt_done'])
        self.assertEqual(self.provider.receipts.await_count, RECEIPT_ATTEMPTS)
        # даже когда срок подошёл, опросов больше нет
        await self.store.pool.execute('UPDATE paykeeper_payments SET next_receipt_at=now() WHERE payment_id=$1',
                                      self.payment_id)
        self.assertFalse(await self.store.receipt_once())
        self.assertEqual(self.provider.receipts.await_count, RECEIPT_ATTEMPTS)
        # гостю не приходит ни пустая ссылка, ни повторяющиеся сообщения
        self.assertFalse(await self.store.pool.fetchval(
            "SELECT EXISTS(SELECT 1 FROM outbox WHERE event LIKE 'pkreceipt:%')"))
        self.assertTrue(await self.store.pool.fetchval(
            "SELECT EXISTS(SELECT 1 FROM outbox WHERE event=$1)", 'pkreceiptmissing:' + self.payment_id))

    async def test_provider_error_ends_as_failed(self):
        self.provider.receipts.side_effect = ProviderError('provider_http_error')
        calls = await self.drain()
        self.assertEqual(calls, RECEIPT_ATTEMPTS)
        p = await self.payment()
        self.assertEqual(p['receipt_state'], 'failed')
        self.assertEqual(p['receipt_error'], 'receipt_check_failed')
        await self.store.pool.execute('UPDATE paykeeper_payments SET next_receipt_at=now() WHERE payment_id=$1',
                                      self.payment_id)
        self.assertFalse(await self.store.receipt_once())

    async def test_manual_recheck_finds_receipt_after_missing(self):
        await self.drain()
        self.assertEqual((await self.payment())['receipt_state'], 'missing')
        self.provider.receipts.side_effect = None
        self.provider.receipts.return_value = [RECEIPT]
        self.assertEqual(await self.store.recheck_receipt(1, self.payment_id), 'formed')
        p = await self.payment()
        self.assertEqual(p['receipt_state'], 'formed')
        self.assertTrue(p['receipt_done'])
        self.assertEqual(await self.store.pool.fetchval(
            'SELECT url FROM paykeeper_fiscal_receipts WHERE payment_id=$1', self.payment_id), RECEIPT['url'])

    async def test_manual_recheck_restarts_auto_polling(self):
        await self.drain()
        self.provider.receipts.side_effect = None
        self.provider.receipts.return_value = []
        self.assertEqual(await self.store.recheck_receipt(1, self.payment_id), 'waiting')
        p = await self.payment()
        self.assertEqual(p['receipt_attempts'], 1)
        self.assertEqual(p['receipt_state'], 'waiting')
        # ручная проверка не нашла чек, но автоматические опросы снова разрешены
        await self.store.pool.execute('UPDATE paykeeper_payments SET next_receipt_at=now() WHERE payment_id=$1',
                                      self.payment_id)
        self.provider.receipts.return_value = [RECEIPT]
        self.assertTrue(await self.store.receipt_once())
        self.assertEqual((await self.payment())['receipt_state'], 'formed')

    async def test_recheck_of_formed_receipt_is_rejected(self):
        self.provider.receipts.return_value = [RECEIPT]
        await self.store.receipt_once()
        with self.assertRaises(RuleError):
            await self.store.recheck_receipt(1, self.payment_id)

    async def test_unmatched_payment_receipt_is_found(self):
        await self.store.pool.execute(
            'INSERT INTO paykeeper_unmatched(payment_id,order_id,client_id,amount) VALUES($1,$2,$3,$4)',
            '7777', 'REQ-unknown', PHONE, 200000)
        self.provider.receipts.return_value = [RECEIPT]
        self.assertTrue(await self.store.unmatched_receipt_once())
        row = await self.store.pool.fetchrow("SELECT * FROM paykeeper_unmatched WHERE payment_id='7777'")
        self.assertEqual(row['receipt_state'], 'formed')
        self.assertEqual(row['receipt_url'], RECEIPT['url'])

    async def test_unmatched_payment_stops_after_limit(self):
        await self.store.pool.execute(
            'INSERT INTO paykeeper_unmatched(payment_id,order_id,client_id,amount) VALUES($1,$2,$3,$4)',
            '7778', 'REQ-unknown-2', '', 200000)
        calls = 0
        while calls < RECEIPT_ATTEMPTS + 5:
            if await self.store.pool.fetchval(
                    "SELECT receipt_state FROM paykeeper_unmatched WHERE payment_id='7778'") != 'waiting':
                break
            await self.store.pool.execute(
                "UPDATE paykeeper_unmatched SET next_receipt_at=now() WHERE payment_id='7778'")
            await self.store.unmatched_receipt_once()
            calls += 1
        self.assertEqual(calls, RECEIPT_ATTEMPTS)
        self.assertEqual(await self.store.pool.fetchval(
            "SELECT receipt_state FROM paykeeper_unmatched WHERE payment_id='7778'"), 'missing')
        await self.store.pool.execute(
            "UPDATE paykeeper_unmatched SET next_receipt_at=now() WHERE payment_id='7778'")
        self.assertFalse(await self.store.unmatched_receipt_once())


@unittest.skipUnless(URL, 'TEST_DATABASE_URL required')
class ReceiptAdmin(Setup):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.client = TestClient(TestServer(make_app(self.store, self.settings)))
        await self.client.start_server()
        # Готовая сессия админки: тот же эффект, что после /admin в боте.
        token, csrf = 's' + secrets.token_hex(16), secrets.token_hex(8)
        await self.store.pool.execute(
            "INSERT INTO web_sessions VALUES($1,$2,$3,now()+interval '1 hour')",
            hashlib.sha256(token.encode()).hexdigest(), 1, csrf)
        self.client.session.cookie_jar.update_cookies({'pihta_session': token})
        self.csrf = csrf

    async def asyncTearDown(self):
        await self.client.close()
        await super().asyncTearDown()

    async def page(self, path):
        response = await self.client.get(path)
        self.assertEqual(response.status, 200)
        return await response.text()

    async def test_booking_page_says_receipt_not_formed(self):
        await self.drain()
        body = await self.page('/booking/' + self.ident)
        self.assertIn('чек не сформирован', body)
        self.assertNotIn('Чек формируется', body)
        self.assertIn('/receipt/' + self.payment_id + '/recheck', body)

    async def test_booking_page_shows_receipt_link(self):
        self.provider.receipts.return_value = [RECEIPT]
        await self.store.receipt_once()
        body = await self.page('/booking/' + self.ident)
        self.assertIn('Открыть чек', body)
        self.assertIn('rc_id=1725', body)

    async def test_recheck_button_starts_check_from_admin(self):
        await self.drain()
        self.provider.receipts.side_effect = None
        self.provider.receipts.return_value = [RECEIPT]
        response = await self.client.post('/receipt/' + self.payment_id + '/recheck?back=' + self.ident,
                                          data={'csrf': self.csrf, 'operation': secrets.token_hex(16)},
                                          headers={'Origin': self.settings.web_url}, allow_redirects=False)
        self.assertEqual(response.status, 302)
        self.assertEqual(response.headers['Location'], '/booking/' + self.ident)
        self.assertEqual((await self.payment())['receipt_state'], 'formed')
        body = await self.page('/booking/' + self.ident)
        self.assertIn('rc_id=1725', body)

    async def test_payments_page_lists_unmatched_payment(self):
        await self.store.pool.execute(
            'INSERT INTO paykeeper_unmatched(payment_id,order_id,client_id,amount) VALUES($1,$2,$3,$4)',
            '7777', 'REQ-unknown', PHONE, 200000)
        self.provider.receipts.return_value = [RECEIPT]
        await self.store.unmatched_receipt_once()
        body = await self.page('/payments')
        self.assertIn('Неопознанные платежи', body)
        self.assertIn('7777', body)
        self.assertIn(PHONE, body)
        self.assertIn('rc_id=1725', body)
