"""Гость из двух мессенджеров: склейка по подтверждённому номеру.

Проверяем решения владельца:
1. склеивает только номер из кнопки «поделиться контактом», введённый руками — нет;
2. занятый номер сначала спрашивает «это вы?»;
3. «Нет» — отказ, номером пользоваться нельзя;
4. сообщения уходят в последний активный мессенджер гостя;
5. смена номера в «Моих данных» сохраняет брони;
6. в админке аккаунт можно привязать и отвязать.
"""
import hashlib
import os
import secrets
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from booking_domain import TZ, RuleError, estimate
from booking_store import BookingStore
from core.app import build_app
from core.workers import deliver_once
from settings import load

URL = os.getenv('TEST_DATABASE_URL', '')
TOKEN = 'x' * 40
PHONE_A = '+79110000011'
PHONE_B = '+79110000022'
PHONE_C = '+79110000033'
PHONE_D = '+79110000044'
TG = 700001
MAX_ID = 800002


class Channel:
    """Ответ канала доставки: тело не нужно, важен только код."""
    status = 200

    async def json(self):
        return {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class Session:
    """Мок транспорта: запоминаем, куда и кому ушло сообщение."""

    def __init__(self):
        self.calls = []

    def post(self, url, json=None, headers=None):
        self.calls.append((url, json or {}, headers or {}))
        return Channel()


class Setup(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = 'test_guests_' + secrets.token_hex(8)
        self.settings = replace(load(), database_url=URL, admin_ids=(1,), web_url='https://core.test')
        self.store = await BookingStore.connect(self.settings, self.schema)
        self.client = TestClient(TestServer(build_app(self.store, self.settings, TOKEN)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.store.pool.execute(f'DROP SCHEMA {self.schema} CASCADE')
        await self.store.pool.close()

    # ---------- помощники ----------

    async def send(self, platform, actor, action=None, text=None, phone=None):
        body = {'platform': platform, 'user_id': actor}
        if action is not None:
            body['action'] = action
            path = '/api/v1/screen'
        elif phone is not None:
            body['phone'] = phone
            path = '/api/v1/contact'
        else:
            body['text'] = text
            path = '/api/v1/text'
        response = await self.client.post(path, headers={'X-Core-Token': TOKEN}, json=body)
        self.assertEqual(response.status, 200, await response.text())
        return await response.json()

    @staticmethod
    def find(screen, prefix):
        for row in screen['rows']:
            for button in row:
                if (button.get('action') or '').startswith(prefix):
                    return button['action']
        raise AssertionError(f'no button {prefix} in {screen["rows"]}')

    async def shared_phone(self, actor, platform, phone, name=''):
        """Кнопка «поделиться контактом»: платформа и время активности + контакт."""
        await self.store.touch(actor, platform)
        return await self.store.confirm_contact(actor, phone, name)

    async def book(self, actor, phone, name='Гость', days=4):
        """Бронь напрямую через store: диалог здесь не проверяем."""
        start = (datetime.now(TZ) + timedelta(days=days)).replace(hour=13, minute=0, second=0, microsecond=0)
        token = await self.store.begin(actor)
        d = await self.store.draft(actor)
        catalog, holidays, rates = await self.store.pricing()
        data = dict(d['data'], name=name, phone=phone, day=start.date().isoformat(),
                    start=start.isoformat(), consent=True, rules_hash=self.settings.rules_hash)
        data['quote'] = estimate(data, catalog, holidays, rates)
        await self.store.save(actor, d, 'review', data)
        return (await self.store.submit(actor, token))[0]

    async def join(self, actor, platform, phone, name=''):
        """Склеиваем аккаунт с гостем по занятому номеру (как ответ «да»)."""
        await self.store.touch(actor, platform)
        result = await self.store.confirm_contact(actor, phone, name)
        self.assertEqual(result['result'], 'conflict')
        await self.store.merge_account_into(actor, result['guest']['id'], name)
        return result['guest']

    # ---------- тесты ----------

@unittest.skipUnless(URL, 'TEST_DATABASE_URL required')
class Guests(Setup):

    async def test_booking_from_telegram_is_visible_in_max(self):
        """(а) Бронь из Telegram видна из MAX-аккаунта того же гостя."""
        await self.shared_phone(TG, 'telegram', PHONE_A, 'Иван')
        ident = await self.book(TG, PHONE_A, 'Иван')

        # MAX: знакомимся, делимся тем же номером, отвечаем «да, это я».
        await self.send('max', MAX_ID, action='begin')
        await self.send('max', MAX_ID, text='Иван')
        question = await self.send('max', MAX_ID, phone=PHONE_A)
        self.assertIn('Это вы?', question['text'])
        merged = await self.send('max', MAX_ID, action=self.find(question, 'guestmerge:yes'))
        self.assertEqual(merged['state'], 'guests')

        mine = await self.send('max', MAX_ID, action='list:0:0')
        self.assertNotIn('Здесь пока пусто', mine['text'])
        card = await self.send('max', MAX_ID, action='open:' + ident)
        self.assertIn('Бронь № ' + ident, card['text'])
        self.assertEqual([r['id'] for r in await self.store.listing(MAX_ID)], [ident])

    async def test_confirmed_contact_links_accounts(self):
        """(б) Подтверждённый контакт создаёт гостя и привязывает аккаунт."""
        result = await self.shared_phone(TG, 'telegram', PHONE_A, 'Иван')
        self.assertEqual(result['result'], 'bound')
        guest = await self.store.guest_of(self.store.pool, TG)
        self.assertEqual(guest['phone'], PHONE_A)
        accounts = await self.store.guest_accounts(guest['id'])
        self.assertEqual([(a['platform'], a['external_id'], a['verified']) for a in accounts],
                         [('telegram', TG, True)])

        # Второй мессенджер с тем же номером видит того же гостя.
        asked = await self.shared_phone(MAX_ID, 'max', PHONE_A)
        self.assertEqual(asked['result'], 'conflict')
        self.assertEqual(asked['guest']['id'], guest['id'])
        self.assertIsNone(await self.store.guest_of(self.store.pool, MAX_ID))

    async def test_typed_phone_never_links_accounts(self):
        """(в) Номер, введённый руками, аккаунты не склеивает."""
        await self.shared_phone(TG, 'telegram', PHONE_A, 'Иван')
        ident = await self.book(TG, PHONE_A, 'Иван')

        # MAX проходит тот же шаг телефона, но печатает номер текстом.
        await self.send('max', MAX_ID, action='begin')
        await self.send('max', MAX_ID, text='Иван')
        typed = await self.send('max', MAX_ID, text=PHONE_A)
        self.assertEqual(typed['state'], 'guests')

        self.assertIsNone(await self.store.guest_of(self.store.pool, MAX_ID))
        self.assertEqual(await self.store.listing(MAX_ID), [])
        with self.assertRaises(RuleError):
            await self.store.get(MAX_ID, ident)

    async def test_taken_phone_asks_and_no_refuses(self):
        """(г) «Нет» — не склеиваем и не даём пользоваться чужим номером."""
        guest = (await self.shared_phone(TG, 'telegram', PHONE_A, 'Иван'))['guest']
        await self.book(TG, PHONE_A, 'Иван')

        await self.send('max', MAX_ID, action='begin')
        await self.send('max', MAX_ID, text='Пётр')
        question = await self.send('max', MAX_ID, phone=PHONE_A)
        self.assertIn('Это вы?', question['text'])
        refused = await self.send('max', MAX_ID, action=self.find(question, 'guestmerge:no'))
        self.assertIn('уже привязан к другому аккаунту', refused['text'])
        self.assertEqual(refused['state'], 'phone_contact')

        self.assertIsNone(await self.store.guest_of(self.store.pool, MAX_ID))
        self.assertEqual([a['external_id'] for a in await self.store.guest_accounts(guest['id'])], [TG])

    async def test_message_goes_to_last_active_messenger(self):
        """(д) Напоминание уходит в мессенджер последнего действия гостя."""
        await self.shared_phone(TG, 'telegram', PHONE_A, 'Иван')
        through_max = await self.book(TG, PHONE_A, 'Иван')
        await self.join(MAX_ID, 'max', PHONE_A, 'Иван')
        # Аккаунт MAX ответил позже — доставка должна уйти туда.
        await self.store.touch(MAX_ID, 'max')
        await self.store.pool.execute('UPDATE outbox SET delivered_at=now() WHERE recipient=1')

        session = Session()
        channels = {'telegram': {'url': 'https://tg.test', 'token': 't'},
                    'max': {'url': 'https://max.test', 'token': 't'}}
        self.assertTrue(await deliver_once(self.store, self.settings, channels, session))

        url, payload, headers = session.calls[-1]
        self.assertEqual(url, 'https://max.test/deliver')
        self.assertEqual(payload['external_id'], MAX_ID)
        self.assertEqual(headers['X-Core-Token'], 't')
        self.assertIsNotNone(await self.store.pool.fetchval(
            'SELECT delivered_at FROM outbox WHERE booking_id=$1 AND event=$2', through_max, 'submitted'))

    async def test_profile_phone_change_keeps_bookings(self):
        """(е) Смена номера в «Моих данных» обновляет гостя, брони остаются."""
        guest = (await self.shared_phone(TG, 'telegram', PHONE_A, 'Иван'))['guest']
        ident = await self.book(TG, PHONE_A, 'Иван')

        step = await self.send('telegram', TG, action='profileedit')
        self.assertEqual(step['state'], 'profile_name')
        step = await self.send('telegram', TG, text='Иван')
        self.assertEqual(step['state'], 'profile_phone')
        screen = await self.send('telegram', TG, text=PHONE_B)
        self.assertIn(PHONE_B, screen['text'])

        guest = await self.store.guest(guest['id'])
        self.assertEqual(guest['phone'], PHONE_B)
        self.assertEqual([r['id'] for r in await self.store.listing(TG)], [ident])
        self.assertIn('Бронь № ' + ident, (await self.send('telegram', TG, action='open:' + ident))['text'])

    async def test_migration_creates_guests_from_profiles_and_bookings(self):
        """Миграция: профили и брони получают гостя по своему номеру."""
        await self.store.pool.execute('INSERT INTO profiles VALUES($1,$2,$3)', 555, 'Профиль', PHONE_C)
        await self.book(556, PHONE_D, 'Пётр')
        await self.store.pool.execute('DELETE FROM profiles WHERE telegram_id=556')
        # Чистим гостей и повторяем схему: миграция должна их восстановить.
        await self.store.pool.execute('DELETE FROM guests')
        await self.store.pool.execute(Path('booking_schema.sql').read_text(encoding='utf-8'))

        by_phone = {r['phone']: r for r in await self.store.pool.fetch('SELECT * FROM guests')}
        self.assertEqual(set(by_phone), {PHONE_C, PHONE_D})
        accounts = {(a['platform'], a['external_id'], a['guest_id'], a['verified'])
                    for a in await self.store.pool.fetch('SELECT * FROM guest_accounts')}
        self.assertEqual(accounts, {('telegram', 555, by_phone[PHONE_C]['id'], True),
                                    ('telegram', 556, by_phone[PHONE_D]['id'], True)})


@unittest.skipUnless(URL, 'TEST_DATABASE_URL required')
class GuestAdmin(Setup):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        token, csrf = 's' + secrets.token_hex(16), secrets.token_hex(8)
        await self.store.pool.execute(
            "INSERT INTO web_sessions VALUES($1,$2,$3,now()+interval '1 hour')",
            hashlib.sha256(token.encode()).hexdigest(), 1, csrf)
        self.client.session.cookie_jar.update_cookies({'pihta_session': token})
        self.csrf = csrf

    async def page(self, path):
        response = await self.client.get(path)
        self.assertEqual(response.status, 200)
        return await response.text()

    async def post(self, path, data):
        response = await self.client.post(path, data={'csrf': self.csrf, **data},
                                          headers={'Origin': self.settings.web_url},
                                          allow_redirects=False)
        self.assertEqual(response.status, 302)
        return response

    async def test_admin_attaches_and_detaches_account(self):
        """(ж) Админка: привязка и отвязка аккаунта в карточке гостя."""
        guest = (await self.shared_phone(TG, 'telegram', PHONE_A, 'Иван'))['guest']
        ident = await self.book(TG, PHONE_A, 'Иван')
        self.assertIn(PHONE_A, await self.page('/guest/' + str(guest['id'])))
        self.assertIn(str(TG), await self.page('/booking/' + ident))

        await self.post('/guest/' + str(guest['id']) + '/attach',
                        {'platform': 'max', 'external_id': str(MAX_ID)})
        self.assertIn(str(MAX_ID), await self.page('/guest/' + str(guest['id'])))
        self.assertEqual(await self.store.pool.fetchval(
            'SELECT guest_id FROM guest_accounts WHERE platform=$1 AND external_id=$2', 'max', MAX_ID),
            guest['id'])
        self.assertTrue(await self.store.pool.fetchval(
            "SELECT EXISTS(SELECT 1 FROM audit WHERE action='guest_attach')"))

        await self.post('/guest/' + str(guest['id']) + '/detach',
                        {'platform': 'max', 'external_id': str(MAX_ID)})
        self.assertFalse(await self.store.pool.fetchval(
            'SELECT EXISTS(SELECT 1 FROM guest_accounts WHERE platform=$1 AND external_id=$2)', 'max', MAX_ID))
        self.assertTrue(await self.store.pool.fetchval(
            "SELECT EXISTS(SELECT 1 FROM audit WHERE action='guest_detach')"))

    async def test_admin_cannot_steal_account_of_other_guest(self):
        """Чужой аккаунт нельзя привязать без отвязки: иначе теряется история."""
        first = (await self.shared_phone(TG, 'telegram', PHONE_A, 'Иван'))['guest']
        second = (await self.shared_phone(MAX_ID, 'max', PHONE_B, 'Пётр'))['guest']
        self.assertNotEqual(first['id'], second['id'])
        response = await self.client.post('/guest/' + str(second['id']) + '/attach',
                                          data={'csrf': self.csrf, 'platform': 'telegram', 'external_id': str(TG)},
                                          headers={'Origin': self.settings.web_url}, allow_redirects=False)
        self.assertEqual(response.status, 400)
        self.assertEqual(await self.store.pool.fetchval(
            'SELECT guest_id FROM guest_accounts WHERE platform=$1 AND external_id=$2', 'telegram', TG),
            first['id'])


@unittest.skipUnless(URL, 'TEST_DATABASE_URL required')
class KnownGuest(Setup):
    async def test_known_guest_is_not_asked_for_name_and_phone_again(self):
        """Имя и телефон уже есть в «Моих данных» — не спрашиваем их снова."""
        actor = 700501
        await self.book(actor, PHONE_A, 'Иван')
        screen = await self.send('telegram', actor, action='begin')
        self.assertEqual(screen['state'], 'guests')
        self.assertIn('Всего гостей', screen['text'])
        self.assertNotIn('Как к Вам обращаться', screen['text'])
        draft = await self.store.draft(actor)
        self.assertEqual(draft['data']['name'], 'Иван')
        self.assertEqual(draft['data']['phone'], PHONE_A)

    async def test_new_guest_still_asked_for_name(self):
        """Новый гость по-прежнему называет себя первым делом."""
        screen = await self.send('telegram', 700502, action='begin')
        self.assertEqual(screen['state'], 'name')
