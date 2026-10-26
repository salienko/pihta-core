"""End-to-end guest journey through the core API: menu -> submit."""
import os
import secrets
import unittest
from dataclasses import replace
from datetime import datetime, timedelta

from aiohttp.test_utils import TestClient, TestServer

from booking_domain import TZ
from booking_store import BookingStore
from core.app import build_app
from settings import load

URL = os.getenv('TEST_DATABASE_URL', '')
TOKEN = 'x' * 40


@unittest.skipUnless(URL, 'TEST_DATABASE_URL is required for PostgreSQL tests')
class GuestJourney(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = 'test_flow_' + secrets.token_hex(8)
        self.settings = replace(load(), database_url=URL, admin_ids=(1,), web_url='https://core.test')
        self.store = await BookingStore.connect(self.settings, self.schema)
        self.client = TestClient(TestServer(build_app(self.store, self.settings, TOKEN)))
        await self.client.start_server()
        self.actor = 530433212

    async def asyncTearDown(self):
        await self.client.close()
        await self.store.pool.execute(f'DROP SCHEMA {self.schema} CASCADE')
        await self.store.pool.close()

    async def act(self, action=None, text=None, **extra):
        body = {'platform': 'max', 'user_id': self.actor, **extra}
        if action is not None:
            body['action'] = action
            response = await self.client.post('/api/v1/screen', headers={'X-Core-Token': TOKEN}, json=body)
        else:
            body['text'] = text
            response = await self.client.post('/api/v1/text', headers={'X-Core-Token': TOKEN}, json=body)
        self.assertEqual(response.status, 200, await response.text())
        return await response.json()

    @staticmethod
    def find(screen, prefix, suffix=None):
        for row in screen['rows']:
            for button in row:
                action = button.get('action') or ''
                if action.startswith(prefix) and (suffix is None or action.endswith(suffix)):
                    return action
        raise AssertionError(f'no button {prefix}/{suffix} in {screen["rows"]}')

    async def test_full_journey_creates_pending_booking(self):
        menu = await self.act('home')
        self.assertIn('ПИХТА', menu['text'])
        self.assertIn('📅 Забронировать', [b['text'] for row in menu['rows'] for b in row])

        name_step = await self.act('begin')
        self.assertEqual(name_step['state'], 'name')
        self.assertIn('Как к Вам обращаться', name_step['text'])

        phone_step = await self.act(text='Иван')
        self.assertEqual(phone_step['state'], 'phone_contact')
        self.assertTrue(phone_step['reply_contact'])

        guests = await self.act(text='+7 981 244-00-50')
        self.assertEqual(guests['state'], 'guests')
        self.assertIn('Всего гостей: 1 чел.', guests['text'])

        plus = self.find(guests, 'guest:', 'paid+')
        guests = await self.act(plus)
        self.assertIn('Всего гостей: 2 чел.', guests['text'])

        calendar_screen = await self.act(self.find(guests, 'next:', 'date'))
        self.assertEqual(calendar_screen['state'], 'date')

        day = (datetime.now(TZ) + timedelta(days=4)).date().isoformat()
        slots = await self.act(self.find(calendar_screen, 'day:', day))
        self.assertEqual(slots['state'], 'slot')

        review = await self.act(self.find(slots, 'slot:'))
        self.assertEqual(review['state'], 'review')
        self.assertIn('Проверьте, все ли верно', review['text'])
        self.assertIn('Общая стоимость составит', review['text'])

        done = await self.act(self.find(review, 'submit:'))
        self.assertIn('Заявка отправлена', done['text'])
        open_action = self.find(done, 'open:')
        ident = open_action.split(':')[1]

        row = await self.store.get(self.actor, ident)
        self.assertEqual(row['status'], 'pending')
        self.assertEqual(row['data']['name'], 'Иван')
        self.assertEqual(row['data']['phone'], '+79812440050')
        self.assertEqual(row['data']['guests'], 2)

        card_screen = await self.act(open_action)
        self.assertIn('Бронь № ' + ident, card_screen['text'])

    async def test_stale_button_is_rejected(self):
        await self.act('begin')
        await self.act(text='Пётр')
        guests = await self.act(text='+79812440051')
        plus = self.find(guests, 'guest:', 'paid+')
        await self.act(plus)
        stale = await self.act(plus)
        self.assertIn('Экран уже изменился', stale['text'])

    async def test_invalid_phone_keeps_step(self):
        await self.act('begin')
        await self.act(text='Пётр')
        response = await self.act(text='не телефон')
        self.assertIn('номер', response['text'].lower())

    async def test_text_endpoint_requires_platform(self):
        response = await self.client.post('/api/v1/text', headers={'X-Core-Token': TOKEN},
                                          json={'user_id': 5, 'text': 'привет'})
        self.assertEqual(response.status, 400)
