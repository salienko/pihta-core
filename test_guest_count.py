"""Количество гостей: счётчики открываются кнопкой, взрослый хотя бы один, вместе до восьми."""
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
class GuestCount(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = 'test_count_' + secrets.token_hex(8)
        self.settings = replace(load(), database_url=URL, admin_ids=(1,), web_url='https://core.test')
        self.store = await BookingStore.connect(self.settings, self.schema)
        self.client = TestClient(TestServer(build_app(self.store, self.settings, TOKEN)))
        await self.client.start_server()
        self.actor = 530433212

    async def asyncTearDown(self):
        await self.client.close()
        await self.store.pool.execute(f'DROP SCHEMA {self.schema} CASCADE')
        await self.store.pool.close()

    async def act(self, action=None, text=None):
        body = {'platform': 'max', 'user_id': self.actor}
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
        return None

    async def guests_screen(self):
        await self.act('begin')
        await self.act(text='Иван')
        return await self.act(text='+79812440061')

    async def test_counter_opens_only_by_button(self):
        screen = await self.guests_screen()
        self.assertIsNone(self.find(screen, 'guest:', 'paid+'), screen['rows'])
        self.assertIsNotNone(self.find(screen, 'focus:', 'paid'))

        opened = await self.act(self.find(screen, 'focus:', 'paid'))
        self.assertIsNotNone(self.find(opened, 'guest:', 'paid+'))
        self.assertIsNone(self.find(opened, 'guest:', 'kid+'))
        self.assertIn('Укажите кол-во', opened['text'])
        self.assertIn('Старше 7 лет: 1 чел.', opened['text'])

        switched = await self.act(self.find(opened, 'focus:', 'kid'))
        self.assertIsNotNone(self.find(switched, 'guest:', 'kid+'))
        self.assertIsNone(self.find(switched, 'guest:', 'paid+'))

    async def test_adult_cannot_drop_to_zero(self):
        screen = await self.guests_screen()
        screen = await self.act(self.find(screen, 'focus:', 'kid'))
        screen = await self.act(self.find(screen, 'guest:', 'kid+'))
        self.assertIn('Всего гостей: 2 чел.', screen['text'])

        screen = await self.act(self.find(screen, 'focus:', 'paid'))
        # один взрослый уже есть — минус недоступен
        self.assertIsNone(self.find(screen, 'guest:', 'paid-'), screen['rows'])
        blocked = await self.act(self.find(screen, 'limit:'))
        self.assertIn('Взрослый гость должен быть хотя бы один', blocked['text'])

        draft = await self.store.draft(self.actor)
        self.assertEqual(draft['data']['guests'], 2)
        self.assertEqual(draft['data']['children'], 1)

    async def test_eight_guests_is_the_cap(self):
        screen = await self.guests_screen()
        screen = await self.act(self.find(screen, 'focus:', 'kid'))
        for _ in range(7):
            screen = await self.act(self.find(screen, 'guest:', 'kid+'))
        self.assertIn('Всего гостей: 8 чел.', screen['text'])
        self.assertIsNone(self.find(screen, 'guest:', 'kid+'), screen['rows'])
        blocked = await self.act(self.find(screen, 'limit:'))
        self.assertIn('до 8 гостей вместе с детьми', blocked['text'])

    async def test_review_keeps_adults_and_kids(self):
        screen = await self.guests_screen()
        screen = await self.act(self.find(screen, 'focus:', 'paid'))
        screen = await self.act(self.find(screen, 'guest:', 'paid+'))
        screen = await self.act(self.find(screen, 'focus:', 'kid'))
        screen = await self.act(self.find(screen, 'guest:', 'kid+'))
        self.assertIn('Всего гостей: 3 чел.', screen['text'])

        screen = await self.act(self.find(screen, 'next:', 'date'))
        day = (datetime.now(TZ) + timedelta(days=4)).date().isoformat()
        action = None
        for _ in range(3):
            action = self.find(screen, 'day:', day)
            if action:
                break
            screen = await self.act(self.find(screen, 'month:'))
        self.assertIsNotNone(action, f'нет дня {day}')
        screen = await self.act(action)
        review = await self.act(self.find(screen, 'slot:'))
        self.assertIn('Кол-во гостей: 3 чел.', review['text'])
        self.assertIn('Старше 7 лет 2 чел', review['text'])
        self.assertIn('Младше 7 лет 1 чел', review['text'])
