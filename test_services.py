"""Доп. услуги: гость меняет состав и количество, админ — ассортимент.

Проверяем то, о чём просил владелец:
1. в действующей брони есть кнопка «Доп. услуги» и список доступных услуг;
2. «+» и «−» меняют количество, сумма брони пересчитывается;
3. услугу можно убрать совсем;
4. нельзя опустить сумму ниже уже внесённой оплаты;
5. админ добавляет новую услугу, и она сразу видна гостю;
6. скрытая услуга гостю не показывается.
"""
import os
import secrets
import unittest
from dataclasses import replace
from datetime import datetime, timedelta

from aiohttp.test_utils import TestClient, TestServer

from admin_web import make_app
from booking_domain import TZ, RuleError, estimate, booking_total
from booking_store import BookingStore
from core.screens import Screens
from settings import load

URL = os.getenv('TEST_DATABASE_URL', '')
PHONE = '+7' '981' '555' '00' '11'
ACTOR = 7001


@unittest.skipUnless(URL, 'TEST_DATABASE_URL is required for PostgreSQL tests')
class Services(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = 'test_services_' + secrets.token_hex(8)
        self.settings = replace(load(), database_url=URL, admin_ids=(ACTOR,),
                                web_url='https://pihta.test')
        self.store = await BookingStore.connect(self.settings, self.schema)
        self.screens = Screens(self.settings, self.store)
        self.client = TestClient(TestServer(make_app(self.store, self.settings, secure=False)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.store.pool.execute(f'DROP SCHEMA {self.schema} CASCADE')
        await self.store.pool.close()

    async def book(self, start):
        token = await self.store.begin(ACTOR)
        d = await self.store.draft(ACTOR)
        catalog, holidays, rates = await self.store.pricing()
        data = dict(d['data'], name='Гость', phone=PHONE, day=start.date().isoformat(),
                    start=start.isoformat(), cart={}, consent=True,
                    rules_hash=self.settings.rules_hash)
        data['quote'] = estimate(data, catalog, holidays, rates)
        await self.store.save(ACTOR, d, 'review', data)
        return (await self.store.submit(ACTOR, token))[0]

    @staticmethod
    def start():
        day = (datetime.now(TZ) + timedelta(days=3)).date()
        while day.weekday() >= 5:
            day += timedelta(days=1)
        return datetime.combine(day, datetime.min.time(), TZ) + timedelta(hours=13)

    async def admin_login(self):
        """Сессия админки для проверки страницы цен."""
        import hashlib
        token, csrf = 's' + secrets.token_hex(16), secrets.token_hex(8)
        await self.store.pool.execute(
            "INSERT INTO web_sessions VALUES($1,$2,$3,now()+interval '1 hour')",
            hashlib.sha256(token.encode()).hexdigest(), ACTOR, csrf)
        self.client.session.cookie_jar.update_cookies({'pihta_session': token})
        return csrf

    def labels(self, screen):
        return [b.get('text') for row in screen.rows for b in row]

    async def test_booking_card_offers_services(self):
        ident = await self.book(self.start())
        screen = await self.screens.dispatch(ACTOR, 'open:' + ident)
        self.assertIn('🧺 Доп. услуги', self.labels(screen))

    async def test_guest_adds_and_removes_service(self):
        ident = await self.book(self.start())
        base = (await self.store.get(ACTOR, ident))['estimate']['total']

        listing = await self.screens.dispatch(ACTOR, 'services:' + ident)
        self.assertIn('🧺 Доп. услуги', listing.text)
        self.assertTrue(any('Веник' in (label or '') for label in self.labels(listing)))

        await self.screens.dispatch(ACTOR, 'svcplus:' + ident + ':birch')
        await self.screens.dispatch(ACTOR, 'svcplus:' + ident + ':birch')
        booking = await self.store.get(ACTOR, ident)
        self.assertEqual(booking['extras']['birch']['qty'], 2)
        self.assertEqual(booking['extras_total'], 2 * 50000)
        self.assertEqual(booking_total(booking), base + 2 * 50000)

        await self.screens.dispatch(ACTOR, 'svcminus:' + ident + ':birch')
        booking = await self.store.get(ACTOR, ident)
        self.assertEqual(booking['extras']['birch']['qty'], 1)

        await self.screens.dispatch(ACTOR, 'svczero:' + ident + ':birch')
        booking = await self.store.get(ACTOR, ident)
        self.assertEqual(booking['extras'], {})
        self.assertEqual(booking['extras_total'], 0)
        self.assertEqual(booking_total(booking), base)

    async def test_services_show_up_in_booking_card(self):
        ident = await self.book(self.start())
        await self.screens.dispatch(ACTOR, 'svcplus:' + ident + ':oak')
        screen = await self.screens.dispatch(ACTOR, 'open:' + ident)
        self.assertIn('Веник дубовый 1 шт. 600 ₽', screen.text)

    async def test_cannot_drop_below_paid(self):
        ident = await self.book(self.start())
        await self.screens.dispatch(ACTOR, 'svcplus:' + ident + ':birch')
        booking = await self.store.get(ACTOR, ident)
        # оплачено больше, чем стоит бронь без услуги: убрать её гость не может
        await self.store.pool.execute('UPDATE bookings SET paid=$2 WHERE id=$1',
                                      ident, booking_total(booking) - 10000)
        screen = await self.screens.dispatch(ACTOR, 'svczero:' + ident + ':birch')
        self.assertIn('меньше внесённой оплаты', screen.text)
        booking = await self.store.get(ACTOR, ident)
        self.assertEqual(booking['extras']['birch']['qty'], 1)

    async def test_hidden_service_is_not_offered(self):
        ident = await self.book(self.start())
        await self.store.pool.execute("UPDATE service_settings SET available=false WHERE code='hat'")
        listing = await self.screens.dispatch(ACTOR, 'svccat:' + ident + ':bath')
        self.assertFalse(any('Шапочка' in (label or '') for label in self.labels(listing)))

    async def test_services_are_split_into_categories(self):
        ident = await self.book(self.start())
        listing = await self.screens.dispatch(ACTOR, 'services:' + ident)
        labels = self.labels(listing)
        self.assertIn('🧹 Веники', labels)
        self.assertIn('💨 Аромапарение', labels)
        self.assertIn('🧴 Банные принадлежности', labels)
        self.assertFalse(any('Веник берёзовый' in (label or '') for label in labels), labels)

        brooms = await self.screens.dispatch(ACTOR, 'svccat:' + ident + ':brooms')
        self.assertIn('🧹 Веники', brooms.text)
        self.assertEqual([b.get('text') for row in brooms.rows for b in row
                          if (b.get('text') or '').startswith('Веник')],
                         ['Веник берёзовый — 500 ₽', 'Веник дубовый — 600 ₽'])

        await self.screens.dispatch(ACTOR, 'svcplus:' + ident + ':oak')
        listing = await self.screens.dispatch(ACTOR, 'services:' + ident)
        self.assertIn('🧹 Веники · выбрано 1', self.labels(listing))

    async def test_existing_services_get_categories_on_start(self):
        """Старые строки без категории дозаполняются при старте ядра."""
        await self.store.pool.execute("UPDATE service_settings SET category=''")
        await self.store.sync_services(self.store.pool)
        self.assertEqual((await self.store.service('birch'))['category'], 'brooms')
        self.assertEqual((await self.store.service('slippers'))['category'], 'bath')

    async def test_admin_adds_service(self):
        csrf = await self.admin_login()
        await self.client.get('/prices')
        response = await self.client.post(
            '/prices/service-add',
            data={'code': 'broom', 'name': 'Веник пихтовый', 'price': '450', 'unit': 'шт.',
                  'category': 'brooms', 'csrf': csrf},
            headers={'Origin': self.settings.web_url}, allow_redirects=False)
        self.assertEqual(response.status, 302)
        row = await self.store.service('broom')
        self.assertEqual(row['name'], 'Веник пихтовый')
        self.assertEqual(row['price'], 45000)
        self.assertEqual(row['category'], 'brooms')
        self.assertTrue(row['available'])

        ident = await self.book(self.start())
        await self.screens.dispatch(ACTOR, 'svcplus:' + ident + ':broom')
        booking = await self.store.get(ACTOR, ident)
        self.assertEqual(booking['extras']['broom']['qty'], 1)

    async def test_admin_rejects_bad_code(self):
        csrf = await self.admin_login()
        await self.client.get('/prices')
        for code in ('', 'ве ник', 'x', '1abc'):
            response = await self.client.post(
                '/prices/service-add',
                data={'code': code, 'name': 'Услуга', 'price': '100', 'csrf': csrf},
                headers={'Origin': self.settings.web_url}, allow_redirects=False)
            self.assertEqual(response.status, 400, code)

    async def test_fully_paid_booking_asks_to_pay_the_extras(self):
        """Бронь оплачена полностью, гость добавил услугу — появляется доплата."""
        ident = await self.book(self.start())
        paid = booking_total(await self.store.get(ACTOR, ident))
        await self.store.pool.execute('UPDATE bookings SET paid=$2 WHERE id=$1', ident, paid)
        card = await self.screens.dispatch(ACTOR, 'open:' + ident)
        self.assertIn('Внесена вся стоимость', card.text)

        await self.screens.dispatch(ACTOR, 'svcplus:' + ident + ':birch')
        card = await self.screens.dispatch(ACTOR, 'open:' + ident)
        labels = self.labels(card)
        self.assertTrue(any('Доплатить — 500 ₽' in (label or '') for label in labels), labels)
        self.assertIn('Веник берёзовый 1 шт. 500 ₽', card.text)
        self.assertIn('Остаток: 500 ₽', card.text)

        request, created = await self.store.request_payment(ACTOR, ident, 'balance')
        self.assertTrue(created)
        self.assertEqual(request['amount'], 50000)

    async def test_confirm_choice_returns_to_booking_with_the_topup(self):
        """На экране услуги есть «Подтвердить выбор»: возвращает в бронь с доплатой."""
        ident = await self.book(self.start())
        paid = booking_total(await self.store.get(ACTOR, ident))
        await self.store.pool.execute('UPDATE bookings SET paid=$2 WHERE id=$1', ident, paid)

        screen = await self.screens.dispatch(ACTOR, 'svcplus:' + ident + ':birch')
        labels = self.labels(screen)
        self.assertIn('✅ Подтвердить выбор', labels)
        self.assertIn('1 шт.', labels)
        self.assertIn('−', labels)
        self.assertIn('+', labels)

        card = await self.screens.dispatch(ACTOR, 'svcdone:' + ident)
        self.assertIn('Бронь № ' + ident, card.text)
        self.assertIn('Веник берёзовый 1 шт. 500 ₽', card.text)
        self.assertIn('Остаток: 500 ₽', card.text)
        self.assertTrue(any('Доплатить — 500 ₽' in (label or '') for label in self.labels(card)))
        self.assertEqual(card.notice, '')

    async def test_counter_changes_do_not_send_extra_messages(self):
        """«+»/«−»/«Очистить» только обновляют экран — никаких сообщений про оплату."""
        ident = await self.book(self.start())
        plus = await self.screens.dispatch(ACTOR, 'svcplus:' + ident + ':birch')
        self.assertEqual(plus.notice, '')
        minus = await self.screens.dispatch(ACTOR, 'svcminus:' + ident + ':birch')
        self.assertEqual(minus.notice, '')
        zero = await self.screens.dispatch(ACTOR, 'svczero:' + ident + ':birch')
        self.assertEqual(zero.notice, '')

    async def test_list_shows_the_same_status_as_the_card(self):
        """В списке броней статус такой же, как в карточке: подтверждено + доплата."""
        ident = await self.book(self.start())
        paid = booking_total(await self.store.get(ACTOR, ident))
        await self.store.pool.execute("UPDATE bookings SET paid=$2,status='confirmed' WHERE id=$1", ident, paid)
        await self.screens.dispatch(ACTOR, 'svcplus:' + ident + ':birch')

        listing = await self.screens.dispatch(ACTOR, 'list:0:0')
        self.assertTrue(any('Подтверждено, требует доплаты' in (label or '')
                            for label in self.labels(listing)), self.labels(listing))
        card = await self.screens.dispatch(ACTOR, 'open:' + ident)
        self.assertIn('Подтверждено, требует доплаты', card.text)

        total = booking_total(await self.store.get(ACTOR, ident))
        await self.store.pool.execute('UPDATE bookings SET paid=$2 WHERE id=$1', ident, total)
        listing = await self.screens.dispatch(ACTOR, 'list:0:0')
        self.assertTrue(any((label or '').endswith('· Подтверждено')
                            for label in self.labels(listing)), self.labels(listing))

    async def test_guest_messages_reach_admins_and_reply_reaches_guest(self):
        """Гость пишет администратору, ответ администратора приходит гостю."""
        ident = await self.book(self.start())
        await self.screens.dispatch(ACTOR, 'chat:' + ident)
        await self.screens.dispatch(ACTOR, 'msg:' + ident)
        sent = await self.screens.text(ACTOR, 'Можно ли прийти на полчаса раньше?')
        self.assertEqual(sent.notice, 'Сообщение отправлено администратору.')

        admin = self.settings.admin_ids[0]
        rows = await self.store.pool.fetch(
            "SELECT booking_id, recipient, text FROM outbox WHERE booking_id=$1 AND event LIKE 'guestmsg:%'", ident)
        self.assertEqual({r['recipient'] for r in rows}, set(self.settings.admin_ids))
        self.assertIn('Можно ли прийти на полчаса раньше?', rows[0]['text'])

        await self.store.staff_note(admin, ident, 'N' + secrets.token_hex(8), 'Да, приходите.')
        note = await self.store.pool.fetchrow(
            "SELECT text FROM outbox WHERE booking_id=$1 AND event LIKE 'note:%'", ident)
        self.assertIn('Сообщение от администратора', note['text'])
        self.assertIn('Да, приходите.', note['text'])

        history = await self.screens.dispatch(ACTOR, 'chat:' + ident)
        self.assertIn('Вы: Можно ли прийти на полчаса раньше?', history.text)
        self.assertIn('Администратор: Да, приходите.', history.text)
