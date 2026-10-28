"""Счёт PayKeeper без поля «имя/клиент» и уведомление админу об оплате.

Проверяем два требования владельца:
1. в счёт не передаётся clientid (раньше туда попадал телефон гостя);
2. админ получает уведомление «Поступила оплата на сумму …, телефон …».
"""
import os
import secrets
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from settings import load
from booking_store import BookingStore
from booking_domain import TZ, estimate
from paykeeper import PayKeeper

URL = os.getenv('TEST_DATABASE_URL', '')
PHONE = '+79810000000'


class InvoicePayload(unittest.IsolatedAsyncioTestCase):
    def settings(self):
        return replace(load(), paykeeper_url='https://paykeeper.example', paykeeper_tax='none')

    async def test_invoice_has_no_clientid(self):
        client = PayKeeper(self.settings())
        client.request = AsyncMock(side_effect=[{'token': 'test'}, {'invoice_id': '1001'}])
        await client.invoice(dict(amount=200000, client_id=PHONE, request_id='req1',
                                  phone=PHONE, email='', payment_type='part_prepay'))
        payload = client.request.await_args_list[1].kwargs['data']
        self.assertNotIn('clientid', payload)
        self.assertEqual(payload['orderid'], 'req1')
        self.assertEqual(payload['client_phone'], PHONE)

    async def test_invoice_status_ignores_clientid(self):
        client = PayKeeper(self.settings())
        invoice = dict(invoice_id='1', request_id='r', amount=200000)
        client.request = AsyncMock(return_value=[dict(id='1', orderid='r', clientid='', pay_amount='2000.00')])
        row = await client.invoice_status(invoice)
        self.assertEqual(row['orderid'], 'r')
        client.request = AsyncMock(return_value=[dict(id='1', orderid='other', pay_amount='2000.00')])
        with self.assertRaises(Exception):
            await client.invoice_status(invoice)


@unittest.skipUnless(URL, 'TEST_DATABASE_URL required')
class PaymentNotice(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = 'test_pkname_' + secrets.token_hex(6)
        self.settings = replace(load(), database_url=URL, admin_ids=(1, 9), web_url='https://pihta.test',
                                paykeeper_enabled=True, paykeeper_url='https://paykeeper.example',
                                paykeeper_secret='synthetic-secret', paykeeper_tax='none',
                                paykeeper_deposit_type='part_prepay', paykeeper_full_type='prepay',
                                paykeeper_balance_type='part_prepay')
        self.store = await BookingStore.connect(self.settings, self.schema)
        self.provider = SimpleNamespace(
            invoice=AsyncMock(return_value=('1001', 'https://paykeeper.example/bill/1001/')),
            invoice_status=AsyncMock(return_value={'status': 'paid', 'paymentid': '123'}),
            receipts=AsyncMock(return_value=[]))
        self.store.provider = lambda: self.provider
        start = (datetime.now(TZ) + timedelta(days=4)).replace(hour=13, minute=0, second=0, microsecond=0)
        token = await self.store.begin(2)
        d = await self.store.draft(2)
        catalog, holidays = await self.store.pricing()
        data = dict(d['data'], name='Гость', phone=PHONE, day=start.date().isoformat(),
                    start=start.isoformat(), consent=True, rules_hash=self.settings.rules_hash)
        data['quote'] = estimate(data, catalog, holidays)
        await self.store.save(2, d, 'review', data)
        self.ident = (await self.store.submit(2, token))[0]
        await self.store.pool.execute('UPDATE outbox SET delivered_at=now()')
        self.req, _ = await self.store.request_payment(2, self.ident, 'deposit')
        await self.store.invoice_once()

    async def asyncTearDown(self):
        await self.store.pool.execute(f'DROP SCHEMA {self.schema} CASCADE')
        await self.store.pool.close()

    async def test_staff_invoice_keeps_phone_out_of_client_field(self):
        await self.store.create_staff_invoice(1, secrets.token_hex(16), PHONE, 200000, 'part_prepay')
        client_id = await self.store.pool.fetchval(
            "SELECT client_id FROM paykeeper_invoices WHERE request_id LIKE 'staff%'")
        self.assertEqual(client_id, '')

    async def test_payment_without_clientid_is_credited(self):
        await self.store.accept_paykeeper(dict(id='123', orderid=self.req['id']), 200000)
        self.assertEqual((await self.store.get(2, self.ident))['paid'], 200000)

    async def test_admin_notice_contains_amount_and_phone(self):
        await self.store.accept_paykeeper(dict(id='123', orderid=self.req['id']), 200000)
        text = await self.store.pool.fetchval("SELECT text FROM outbox WHERE event='pkadmin:123'")
        self.assertIn('Поступила оплата на сумму', text)
        self.assertIn(PHONE, text)
