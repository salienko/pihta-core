"""Цены и услуги в БД и раздел админки «Цены и услуги».

Проверяем: цены читаются из БД, пустые таблицы заполняются сидом из content.json,
правка цены в админке меняет цену только новых броней, форма не отдаёт 500.
"""
import hashlib
import os
import secrets
import unittest
from dataclasses import replace
from datetime import datetime, timedelta

from aiohttp.test_utils import TestClient, TestServer

from admin_web import make_app
from booking_domain import TZ, DEFAULT_RATES, RuleError, estimate
from booking_store import BookingStore
from settings import load

URL = os.getenv('TEST_DATABASE_URL', '')
PHONE = '+7' '981' '555' '00' '11'


@unittest.skipUnless(URL, 'TEST_DATABASE_URL is required for PostgreSQL tests')
class Prices(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = 'test_prices_' + secrets.token_hex(8)
        self.settings = replace(load(), database_url=URL, admin_ids=(1,),
                                web_url='https://pihta.test')
        self.store = await BookingStore.connect(self.settings, self.schema)
        self.client = TestClient(TestServer(make_app(self.store, self.settings, secure=False)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.store.pool.execute(f'DROP SCHEMA {self.schema} CASCADE')
        await self.store.pool.close()

    async def login(self):
        """Готовая сессия админки: страницы проверяют её так же, как после /admin в боте."""
        token, csrf = 's' + secrets.token_hex(16), secrets.token_hex(8)
        await self.store.pool.execute(
            "INSERT INTO web_sessions VALUES($1,$2,$3,now()+interval '1 hour')",
            hashlib.sha256(token.encode()).hexdigest(), 1, csrf)
        self.client.session.cookie_jar.update_cookies({'pihta_session': token})
        return csrf

    async def post(self, path, data, csrf):
        return await self.client.post(path, data=dict(data, csrf=csrf),
                                      headers={'Origin': self.settings.web_url},
                                      allow_redirects=False)

    async def book(self, actor, start):
        """Бронь через обычный путь бота: цена фиксируется в снимке брони."""
        token = await self.store.begin(actor)
        d = await self.store.draft(actor)
        catalog, holidays, rates = await self.store.pricing()
        data = dict(d['data'], name='Гость', phone=PHONE, day=start.date().isoformat(),
                    start=start.isoformat(), cart={}, consent=True,
                    rules_hash=self.settings.rules_hash)
        data['quote'] = estimate(data, catalog, holidays, rates)
        await self.store.save(actor, d, 'review', data)
        return (await self.store.submit(actor, token))[0]

    @staticmethod
    def weekday(days):
        """Ближайший будний день не раньше чем через `days` дней, днём."""
        day = (datetime.now(TZ) + timedelta(days=days)).date()
        while day.weekday() >= 5:
            day += timedelta(days=1)
        return datetime.combine(day, datetime.min.time(), TZ) + timedelta(hours=13)

    @classmethod
    def weekdays(cls, count=2):
        """Несколько разных будних дней подряд — по одной брони на дату."""
        day = (datetime.now(TZ) + timedelta(days=3)).date()
        found = []
        while len(found) < count:
            if day.weekday() < 5:
                found.append(datetime.combine(day, datetime.min.time(), TZ) + timedelta(hours=13))
            day += timedelta(days=1)
        return found

    async def test_prices_are_seeded_from_content(self):
        catalog, holidays, rates = await self.store.pricing()
        self.assertEqual(set(catalog), {i['code'] for i in self.settings.content['services']})
        self.assertEqual(catalog['birch']['price'], 50000)
        self.assertTrue(catalog['birch']['available'])
        self.assertEqual(rates, DEFAULT_RATES)
        stored = await self.store.pool.fetchval('SELECT count(*) FROM service_settings WHERE price IS NOT NULL')
        self.assertEqual(stored, len(self.settings.content['services']))

    async def test_prices_come_from_database(self):
        await self.store.pricing()
        await self.store.pool.execute("UPDATE service_settings SET price=70000,available=false WHERE code='birch'")
        catalog, _, _ = await self.store.pricing()
        self.assertEqual(catalog['birch']['price'], 70000)
        self.assertFalse(catalog['birch']['available'])
        with self.assertRaises(RuleError):
            estimate(dict(start=self.weekday(3).isoformat(), hours=2, guests=1, children=0,
                          cart={'birch': 1}), catalog, {}, {})
        await self.store.pool.execute("UPDATE service_settings SET available=true WHERE code='birch'")
        catalog, _, _ = await self.store.pricing()
        quote = estimate(dict(start=self.weekday(3).isoformat(), hours=2, guests=1, children=0,
                              cart={'birch': 1}), catalog, {}, {})
        self.assertEqual(quote['items'][0]['unit_price'], 70000)

    async def test_empty_tables_are_reseeded(self):
        await self.store.pool.execute('DELETE FROM price_settings')
        await self.store.pool.execute('DELETE FROM service_settings')
        catalog, _, rates = await self.store.pricing()
        self.assertEqual(rates, DEFAULT_RATES)
        self.assertEqual(catalog['towel']['price'], 10000)
        self.assertEqual(await self.store.pool.fetchval('SELECT count(*) FROM price_settings'), 3)

    async def test_admin_page_lists_prices(self):
        await self.login()
        response = await self.client.get('/prices')
        self.assertEqual(response.status, 200)
        body = await response.text()
        self.assertIn('Цены и услуги', body)
        self.assertIn('Веник берёзовый', body)
        self.assertIn('500 ₽', body)
        self.assertIn('5 000 ₽', body)

    async def test_admin_page_requires_login(self):
        response = await self.client.get('/prices', allow_redirects=False)
        self.assertEqual(response.status, 302)
        self.assertEqual(response.headers['Location'], '/login')

    async def test_admin_changes_service(self):
        csrf = await self.login()
        self.assertEqual((await self.client.get('/prices')).status, 200)
        response = await self.post('/prices/service/birch',
                                   {'name': 'Веник берёзовый', 'price': '700.00'}, csrf)
        self.assertEqual(response.status, 302)
        self.assertEqual(response.headers['Location'], '/prices')
        row = await self.store.service('birch')
        self.assertEqual(row['price'], 70000)
        self.assertFalse(row['available'])
        self.assertEqual(await self.store.pool.fetchval("SELECT count(*) FROM audit WHERE action='service_price'"), 1)
        body = await (await self.client.get('/prices')).text()
        self.assertIn('700 ₽', body)

    async def test_admin_rejects_bad_price(self):
        csrf = await self.login()
        await self.client.get('/prices')
        for price in ('', '0', '-100', 'дорого'):
            response = await self.post('/prices/service/birch',
                                       {'name': 'Веник берёзовый', 'price': price}, csrf)
            self.assertEqual(response.status, 400, price)
            self.assertIn('Действие не выполнено', await response.text())
        self.assertEqual((await self.store.service('birch'))['price'], 50000)

    async def test_admin_rate_validation(self):
        csrf = await self.login()
        await self.client.get('/prices')
        # тариф за 2 часа делится пополам, поэтому копейки должны быть чётными
        self.assertEqual((await self.post('/prices/rate/rent_weekday', {'amount': '5000.01'}, csrf)).status, 400)
        self.assertEqual((await self.post('/prices/rate/unknown', {'amount': '5000'}, csrf)).status, 400)
        self.assertEqual((await self.post('/prices/rate/guest_extra', {'amount': '300.50'}, csrf)).status, 302)
        _, _, rates = await self.store.pricing()
        self.assertEqual(rates['guest_extra'], 30050)

    async def test_new_price_does_not_touch_existing_booking(self):
        old_start, new_start = self.weekdays()
        old = await self.book(2, old_start)
        before = (await self.store.get(2, old))['estimate']['total']
        self.assertEqual(before, 500000)

        csrf = await self.login()
        self.assertEqual((await self.post('/prices/rate/rent_weekday', {'amount': '6000.00'}, csrf)).status, 302)

        self.assertEqual((await self.store.get(2, old))['estimate']['total'], before)
        fresh = await self.book(3, new_start)
        self.assertEqual((await self.store.get(3, fresh))['estimate']['total'], 600000)
