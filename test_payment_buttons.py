"""The pay button becomes the invoice link once the invoice exists."""
import os
import secrets
import unittest
from dataclasses import replace

from aiohttp.test_utils import TestClient, TestServer

from booking_store import BookingStore
from core.app import build_app
from settings import load

URL = os.getenv('TEST_DATABASE_URL', '')
TOKEN = 'x' * 40
H = {'X-Core-Token': TOKEN}


def find(screen, prefix):
    for row in screen['rows']:
        for b in row:
            action = b.get('action') or ''
            if action.startswith(prefix):
                return action
    raise AssertionError('нет кнопки ' + prefix)


@unittest.skipUnless(URL, 'TEST_DATABASE_URL is required for PostgreSQL tests')
class PayButton(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = 'test_pay_' + secrets.token_hex(8)
        self.settings = replace(load(), database_url=URL, admin_ids=(1,),
                                web_url='https://core.test', paykeeper_enabled=True)
        self.store = await BookingStore.connect(self.settings, self.schema)
        self.client = TestClient(TestServer(build_app(self.store, self.settings, TOKEN)))
        await self.client.start_server()
        self.actor = 4242
        self.ident = await self.booking()

    async def asyncTearDown(self):
        await self.client.close()
        await self.store.pool.execute(f'DROP SCHEMA {self.schema} CASCADE')
        await self.store.pool.close()

    async def post(self, path, body):
        response = await self.client.post(path, headers=H, json=body)
        self.assertEqual(response.status, 200)
        return await response.json()

    async def screen(self, action):
        return await self.post('/api/v1/screen', {'platform': 'telegram',
                                                  'user_id': self.actor, 'action': action})

    async def booking(self):
        await self.screen('begin')
        await self.post('/api/v1/text', {'platform': 'telegram', 'user_id': self.actor, 'text': 'Иван'})
        await self.post('/api/v1/contact', {'platform': 'telegram', 'user_id': self.actor,
                                            'phone': '+79812440099'})
        s = await self.screen(find(await self.screen('resume'), 'next:'))
        s = await self.screen(find(s, 'day:'))
        s = await self.screen(find(s, 'slot:'))
        s = await self.screen(find(s, 'submit:'))
        return find(s, 'open:').split(':')[1]

    async def link_invoice(self, kind, amount, paid_before):
        request = 'REQ' + secrets.token_hex(4)
        await self.store.pool.execute(
            "INSERT INTO payment_requests(id,booking_id,kind,amount,paid_before,state,url,revision) "
            "VALUES($1,$2,$3,$4,$5,'linked',$6,1)",
            request, self.ident, kind, amount, paid_before, 'https://pay.test/' + request)
        await self.store.pool.execute(
            "INSERT INTO paykeeper_invoices(request_id,booking_id,actor,amount,paid_before,client_id,"
            "phone,email,payment_type,invoice_id,url,state) "
            "VALUES($1,$2,$3,$4,$5,$6,$7,$8,'full',$9,$10,'ready')",
            request, self.ident, self.actor, amount, paid_before, 'client-' + request,
            '+79812440099', 'guest@test.local', 'INV' + request, 'https://pay.test/' + request)
        return request

    def buttons(self, screen):
        return [b for row in screen['rows'] for b in row]

    async def test_deposit_button_becomes_link(self):
        request = await self.link_invoice('deposit', 200000, 0)
        screen = await self.screen('open:' + self.ident)
        labels = [b['text'] for b in self.buttons(screen)]
        self.assertIn('🔗 Перейти к оплате 2 000 ₽', labels)
        self.assertNotIn('💳 Внести предоплату 2 000 ₽', labels)
        link = [b for b in self.buttons(screen) if b['text'] == '🔗 Перейти к оплате 2 000 ₽'][0]
        self.assertEqual(link.get('url'), 'https://pay.test/' + request)
        self.assertIsNone(link.get('action'))
        # the full-amount button is untouched and still orders its own invoice
        self.assertIn('payrequest:full:' + self.ident,
                      [b.get('action') for b in self.buttons(screen)])

    async def test_balance_button_becomes_link_after_deposit_paid(self):
        await self.store.pool.execute(
            "UPDATE bookings SET paid=200000 WHERE id=$1", self.ident)
        await self.link_invoice('balance', 400000, 200000)
        screen = await self.screen('open:' + self.ident)
        labels = [b['text'] for b in self.buttons(screen)]
        self.assertIn('🔗 Перейти к оплате 4 000 ₽', labels)
        self.assertNotIn('💳 Оплатить — 4 000 ₽', labels)

    async def test_check_button_stays(self):
        await self.link_invoice('deposit', 200000, 0)
        screen = await self.screen('open:' + self.ident)
        actions = [b.get('action') or '' for b in self.buttons(screen)]
        self.assertTrue(any(a.startswith('paycheck:') for a in actions))
