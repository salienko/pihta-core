"""/health must report the release the deployment actually runs."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer

from admin_web import make_app


class HealthRelease(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        store = SimpleNamespace(pool=SimpleNamespace(fetchval=AsyncMock(return_value=1)))
        settings = SimpleNamespace(paykeeper_enabled=False, paykeeper_settlement_enabled=False,
                                   web_url='https://core.test')
        self.client = TestClient(TestServer(make_app(store, settings)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_release_comes_from_environment(self):
        with patch.dict(os.environ, {'RELEASE': 'test-release-42'}):
            body = await (await self.client.get('/health')).json()
        self.assertEqual(body['status'], 'ok')
        self.assertEqual(body['release'], 'test-release-42')

    async def test_release_fallback_when_environment_unset(self):
        env = {k: v for k, v in os.environ.items() if k not in ('RELEASE', 'CORE_RELEASE')}
        with patch.dict(os.environ, env, clear=True):
            body = await (await self.client.get('/health')).json()
        self.assertEqual(body['status'], 'ok')
        self.assertTrue(body['release'])
        self.assertNotEqual(body['release'], 'rental-v3')
