"""Smoke tests for the core service surface (no messenger involved)."""
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


@unittest.skipUnless(URL, 'TEST_DATABASE_URL is required for PostgreSQL tests')
class CoreApi(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = 'test_core_' + secrets.token_hex(8)
        self.settings = replace(load(), database_url=URL, admin_ids=(1,),
                                web_url='https://core.test')
        self.store = await BookingStore.connect(self.settings, self.schema)
        self.client = TestClient(TestServer(build_app(self.store, self.settings, TOKEN)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.store.pool.execute(f'DROP SCHEMA {self.schema} CASCADE')
        await self.store.pool.close()

    async def test_health_reports_database(self):
        response = await self.client.get('/health')
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual(body['status'], 'ok')
        status = await (await self.client.get('/api/status')).json()
        self.assertTrue(status['db'])
        self.assertEqual(status['service'], 'pihta-core')

    async def test_api_requires_token(self):
        response = await self.client.post('/api/v1/screen', json={'platform': 'max', 'user_id': 5})
        self.assertEqual(response.status, 401)

    async def test_menu_screen_for_both_platforms(self):
        for platform in ('telegram', 'max'):
            response = await self.client.post('/api/v1/screen', headers={'X-Core-Token': TOKEN},
                                              json={'platform': platform, 'user_id': 530433212})
            self.assertEqual(response.status, 200)
            body = await response.json()
            self.assertIn('ПИХТА', body['text'])
            self.assertEqual(body['platform'], platform)
            labels = [b['text'] for row in body['rows'] for b in row]
            self.assertIn('📅 Забронировать', labels)
            self.assertIn('💰 Цены', labels)
            # an admin id sees the panel entry, a guest does not
            self.assertNotIn('🔐 Панель администратора', labels)

    async def test_info_sections(self):
        response = await self.client.post('/api/v1/screen', headers={'X-Core-Token': TOKEN},
                                          json={'platform': 'telegram', 'user_id': 7, 'action': 'info:Цены'})
        body = await response.json()
        self.assertIn('Аренда бани', body['text'])
        self.assertEqual(body['rows'][0][0]['action'], 'begin')

    async def test_bad_platform_rejected(self):
        response = await self.client.post('/api/v1/screen', headers={'X-Core-Token': TOKEN},
                                          json={'platform': 'vk', 'user_id': 7})
        self.assertEqual(response.status, 400)

    async def test_contact_endpoint_accepts_shared_phone(self):
        """The messenger contact button posts here; a 404 here broke the flow."""
        headers = {'X-Core-Token': TOKEN}
        await self.client.post('/api/v1/screen', headers=headers,
                               json={'platform': 'telegram', 'user_id': 777, 'action': 'begin'})
        await self.client.post('/api/v1/text', headers=headers,
                               json={'platform': 'telegram', 'user_id': 777, 'text': 'Иван'})
        response = await self.client.post('/api/v1/contact', headers=headers,
                                          json={'platform': 'telegram', 'user_id': 777,
                                                'phone': '+79812440011'})
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual(body['state'], 'guests')
        self.assertTrue(body['rows'])

    async def test_contact_endpoint_requires_token(self):
        response = await self.client.post('/api/v1/contact',
                                          json={'platform': 'max', 'user_id': 5, 'phone': '+79812440011'})
        self.assertEqual(response.status, 401)
