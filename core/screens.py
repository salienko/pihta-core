"""Transport-neutral screens for the whole guest journey.

The core builds text + button rows; a transport (Telegram, MAX) renders them
with its own keyboard API and sends the pressed action back. No messenger
imports are allowed in this module.

Button rows are tuples of dicts:
    {"text": "label", "action": "begin"}      -> transport sends action back
    {"text": "label", "url": "https://..."}   -> transport opens the link
    {"text": "label", "contact": True}        -> transport offers its contact button

Action strings keep the Telegram callback grammar
(``action:token:revision:value``), so button semantics do not change.
"""
import asyncio
import calendar
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from booking_domain import (TZ, SLOTS, LABELS, RuleError, estimate, card, can_cancel,
                            validate_start, clean_name, normalize_phone, horizon, rub)
from booking_flow import (VERSION, prepare, require, first_missing, change_guests,
                          callback, collected_fields, accept_field)
from booking_payments import total as payment_total


@dataclass(frozen=True)
class Screen:
    text: str = ''
    photo: str = ''
    rows: tuple = field(default_factory=tuple)
    state: str = ''
    notice: str = ''
    reply_contact: bool = False
    nothing: bool = False

    def json(self):
        return {
            "text": self.text,
            "photo": self.photo or None,
            "rows": [list(row) for row in self.rows],
            "state": self.state or None,
            "notice": self.notice or None,
            "reply_contact": self.reply_contact,
        }


def btn(text, action=None, url=None, contact=False):
    if url:
        return {"text": text, "url": url}
    if contact:
        return {"text": text, "contact": True}
    return {"text": text, "action": action}


def nav():
    return ((btn('🏠 Главное меню', 'home'),),)


def rows(*items):
    return tuple(tuple(row) for row in items)


class Screens:
    """Builds screens for one actor and applies the button/text actions."""

    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.locks = defaultdict(asyncio.Lock)

    # ---------- helpers ----------

    async def unfinished(self, actor):
        try:
            return await self.store.draft(actor)
        except RuleError:
            return None

    async def cancel_profile(self, actor):
        d = await self.unfinished(actor)
        if d and d['state'].startswith('profile_'):
            data = dict(d['data'])
            previous = data.pop('profile_return', 'name')
            data.pop('profile_name', None)
            await self.store.save(actor, d, previous, data)

    def link(self, d, action, value=''):
        return callback(action, d, value)

    async def render(self, actor, d):
        state, data = prepare(d['state'], d['data'])
        if state != d['state'] or data != d['data']:
            await self.store.save(actor, d, state, data)
            d = await self.store.draft(actor, d['token'])
        state, data = d['state'], d['data']
        if '_filled' not in data:
            data['_filled'] = collected_fields(state, data)
            await self.store.save(actor, d, state, data)
            d = await self.store.draft(actor, d['token'])
        if state in ('phone_contact', 'profile_contact'):
            state = 'phone' if state == 'phone_contact' else 'profile_phone'
        b = lambda action, value='': callback(action, d, value)
        if state.startswith('profile_'):
            key = 'name' if state == 'profile_name' else 'phone'
            if key == 'phone':
                return self.phone_screen(d, '👤 Мои данные\n\nУкажите телефон для будущих бронирований.', profile=True)
            text = '👤 Мои данные\n\nВведите новое имя.'
            return Screen(text, rows=rows([btn('← Назад', 'profilecancel')]) + nav(), state=state)
        try:
            require(data, state)
        except RuleError:
            state = first_missing(data)
            data['consent'] = False
            await self.store.save(actor, d, state, data)
            d = await self.store.draft(actor, d['token'])
            b = lambda action, value='': callback(action, d, value)
        footer = rows([btn('✖ Отменить оформление', b('discardask'))]) + nav()
        if state == 'name':
            return Screen('👤 Как к Вам обращаться?\n\nНапишите Ваше имя ↓', photo='booking', rows=nav(), state=state)
        if state == 'phone':
            return self.phone_screen(d, '📱 Как с вами связаться?\n\nУкажите ваш номер телефона ↓')
        if state == 'guests':
            paid = data['guests'] - data['children']
            kids = data['children']

            def count(category, delta, enabled):
                label = '−' if delta < 0 else '+'
                return btn(label, b('guest', category + ('+' if delta > 0 else '-')) if enabled
                           else b('limit', 'min' if delta < 0 else 'max'))

            body = rows([btn('Гости старше 7 лет', b('noop'))],
                        [count('paid', -1, paid > 0 and data['guests'] > 1),
                         btn(f'{paid} чел.', b('noop')),
                         count('paid', 1, data['guests'] < 8)],
                        [btn('Гости младше 7 лет', b('noop'))],
                        [count('kid', -1, kids > 0 and data['guests'] > 1),
                         btn(f'{kids} чел.', b('noop')),
                         count('kid', 1, data['guests'] < 8)],
                        [btn('📅 Выбрать дату', b('next', 'date'))],
                        [btn('← Назад', b('back', 'phone'))])
            text = (f'👥 Уточните какое кол-во гостей планирует отдыхать?\n\n'
                    f'Всего гостей: {data["guests"]} чел.\n\nУкажите кол-во гостей ↓')
            return Screen(text, photo='guests', rows=body + footer, state=state)
        if state == 'date':
            today = datetime.now(TZ).date()
            last = horizon(today)
            month = date.fromisoformat(data.get('month', today.replace(day=1).isoformat()))
            if month < today.replace(day=1) or month > last.replace(day=1):
                month = today.replace(day=1)
            months = ('Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь', 'Июль',
                      'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь')
            body = [[btn(f'{months[month.month - 1]} {month.year}', b('noop'))],
                    [btn(day, b('noop')) for day in ('Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс')]]
            occupied = {r['starts_at'].astimezone(TZ).date() for r in await self.store.listing(actor)}
            weeks = calendar.Calendar().monthdatescalendar(month.year, month.month)
            available = await self.store.available_days(
                [day for week in weeks for day in week if day.month == month.month and today <= day <= last])
            for week in weeks:
                body.append([btn(str(day.day), b('day', day.isoformat()))
                             if day.month == month.month and today <= day <= last
                             and day not in occupied and day in available
                             else btn('·', b('noop')) for day in week])
            target = (month + timedelta(days=32)).replace(day=1)
            if target > last.replace(day=1):
                target = today.replace(day=1)
            if target != month:
                body.append([btn('Месяц >', b('month', target.isoformat()))])
            body.append([btn('← Назад', b('back', 'guests'))])
            return Screen('📅 Выберите удобную дату\n\nВыберите день в календаре ↓',
                          photo='date', rows=rows(*body) + footer, state=state)
        if state == 'slot':
            day = date.fromisoformat(data['day'])
            body = []
            for hour in SLOTS:
                start = datetime.combine(day, datetime.min.time(), TZ) + timedelta(hours=hour)
                try:
                    available = await self.store.free(start)
                except RuleError:
                    continue
                if available:
                    body.append([btn(f'{hour:02}:00 - {(hour + 2) % 24:02}:00', b('slot', str(hour)))])
            text = '🕒 Выберите удобное время' if body else 'Свободных сеансов нет.\nВыберите другую дату.'
            body += [[btn('← Назад', b('back', 'date'))], [btn('💬 Другое время', 'contact')]]
            return Screen(text, photo='time', rows=rows(*body) + footer, state=state)
        if state == 'review':
            catalog, holidays = await self.store.pricing()
            try:
                validate_start(datetime.fromisoformat(data['start']), datetime.now(TZ))
                if not await self.store.free(datetime.fromisoformat(data['start'])):
                    raise RuleError('Время уже занято. Выберите другое.')
                q = estimate(data, catalog, holidays)
            except RuleError as error:
                await self.store.save(actor, d, 'slot', dict(data, consent=False))
                d = await self.store.draft(actor, d['token'])
                return Screen(str(error), rows=rows(
                    [btn('🕒 Выбрать время', callback('resume_step', d))],
                    [btn('☎️ Администратор', 'contact')]) + nav(), state='slot')
            if data.get('quote') != q or data.get('rules_hash') != self.settings.rules_hash:
                data['consent'] = False
            data.update(quote=q, rules_hash=self.settings.rules_hash, cart={})
            await self.store.save(actor, d, 'review', data)
            d = await self.store.draft(actor, d['token'])
            b = lambda action, value='': callback(action, d, value)
            start = datetime.fromisoformat(data['start'])
            end = start + timedelta(hours=2)
            text = (f'📋 Проверьте, все ли верно?\n\n👤 {data["name"]}\n📱 {data["phone"]}\n'
                    f'👥 Кол-во гостей: {data["guests"]} чел.\n'
                    f'Старше 7 лет {data["guests"] - data["children"]} чел\n'
                    f'Младше 7 лет {data["children"]} чел\n'
                    f'📅 Дата: {start:%d.%m.%Y}\n🕒 Время {start:%H:%M}–{end:%H:%M}\n\n'
                    f'Общая стоимость составит {rub(q["total"])}\n\nНажмите для подтверждения ↓\n\n'
                    'Подтверждая бронирование, вы соглашаетесь с правилами.')
            return Screen(text, photo='booking', state=state, rows=rows(
                [btn('✅ Подтвердить бронирование', b('submit'))],
                [btn('← Назад', b('back', 'slot'))]) + footer)
        return Screen('Откройте текущее бронирование.', rows=nav(), state=state)

    def phone_screen(self, d, text, profile=False):
        if not profile and d['data'].get('phone'):
            b = lambda action, value='': callback(action, d, value)
            return Screen(text, photo='booking', state='phone', rows=rows(
                [btn('📱 Использовать мой номер', b('keep', 'phone'))],
                [btn('← Назад', b('back', 'name'))],
                [btn('✖ Отменить оформление', b('discardask'))]) + nav())
        return Screen(text, photo='booking', state='profile_phone' if profile else 'phone_contact',
                      reply_contact=True, rows=rows([btn('← Назад', 'profilecancel' if profile else 'resume')]) + nav())

    async def home(self, actor):
        await self.cancel_profile(actor)
        body = [[btn('📅 Забронировать', 'begin')],
                [btn('📋 Мои бронирования', 'list:0:0')],
                [btn('💰 Цены', 'info:Цены'), btn('📖 Правила', 'info:Правила')],
                [btn('📍 Контакты', 'info:Контакты'), btn('❓ Помощь', 'info:Помощь')]]
        if await self.unfinished(actor):
            body.insert(0, [btn('✏️ Продолжить бронирование', 'resume')])
        if actor in self.settings.admin_ids:
            body.append([btn('🔐 Панель администратора', 'staffweb')])
        return Screen('ПИХТА · баня на дровах\n\nБаня целиком для вашей компании.\nВыберите действие.',
                      photo='home', rows=rows(*body))

    async def info(self, actor, section, back=None):
        c = self.settings.content
        body = []
        if back:
            body.append([btn('← Назад', back)])
        if section == 'Контакты':
            text = (f'📍 Как нас найти\n\n{c["name"]}\nПосёлок Приладожское\n\n'
                    f'☎️ {c["phone"]}\nНестандартное время — по согласованию с администратором.')
            body = [[btn('📍 Построить маршрут', url=c['map_url'])]] + body
        elif section == 'Правила':
            text = ('📖 Правила бронирования\n\nДо 8 гостей вместе с детьми.\n'
                    'Дети до 7 лет включительно — без доплаты.\nПростыня на каждого гостя включена.\n\n'
                    'Для подтверждения внесите предоплату 2 000 ₽ или всю стоимость. Остаток — до начала сеанса.\n\n'
                    'Заявка занимает время и ждёт решения администратора.\n'
                    'Неоплаченную ожидающую заявку можно отменить самостоятельно. '
                    'Подтверждённую или оплаченную отменяет администратор по вашему запросу.\n\n'
                    'Отправленную бронь нельзя изменить: отмените её и создайте новую. Опоздание не продлевает сеанс.\n\n'
                    'Не более двух действующих броней на разные даты. Запись за час — два месяца до сеанса.')
            body.insert(0, [btn('📄 Полные правила', 'rulesfull')])
        elif section in ('Цены', 'Цены и услуги'):
            text = ('💰 Аренда бани · 2 часа\n\nПн–пт до 19:00 — 5 000 ₽\n'
                    'С 19:00 и в выходные — 6 000 ₽\nПраздничные даты — 6 500 ₽*\n\n'
                    'Включены 4 гостя от 8 лет.\nЗа каждого следующего — 250 ₽/час.\n'
                    'Дети до 7 лет включительно — бесплатно.\n\n'
                    'Точная сумма будет перед отправкой.\n'
                    '*Особые даты и нестандартная длительность — через администратора.')
            body = [[btn('📅 Забронировать', 'begin')]] + body
        else:
            text = ('❓ Как записаться\n\nИмя → телефон → гости → дата → время.\n'
                    'Проверьте карточку и отправьте её администратору.\n\n'
                    'Черновик сохраняется: можно вернуться позже.\n'
                    'В «Моих бронированиях» — статус, детали и отмена.\n\n'
                    'Нужно другое время или помощь? Свяжитесь с администратором.')
            body.append([btn('☎️ Администратор', 'contact')])
        return Screen(text, photo='home', rows=rows(*body) + nav())

    async def start(self, actor):
        d = await self.unfinished(actor)
        if d:
            return Screen('✏️ У вас есть незавершённое бронирование.\n\n'
                          'Продолжить с сохранённого шага или начать заново?',
                          rows=rows([btn('Продолжить', 'resume')],
                                    [btn('Начать заново', callback('resetask', d))]) + nav())
        if len(await self.store.listing(actor)) >= 2:
            return Screen('У вас уже две действующие брони.\nОткройте их, чтобы посмотреть детали или отменить.',
                          rows=rows([btn('📋 Мои бронирования', 'list:0:0')]) + nav())
        token = await self.store.begin(actor)
        return await self.render(actor, await self.store.draft(actor, token))

    async def listing(self, actor, history=False, offset=0):
        await self.cancel_profile(actor)
        found = await self.store.listing(actor, history, offset=offset)
        body = []
        for r in found[:20]:
            body.append([btn(f'{r["starts_at"].astimezone(TZ):%d.%m · %H:%M} · {LABELS[r["status"]]}',
                             'open:' + r['id'])])
        if len(found) > 20:
            body.append([btn('Далее →', f'list:{int(history)}:{offset + 20}')])
        if offset:
            body.append([btn('← Назад', f'list:{int(history)}:{max(0, offset - 20)}')])
        if history and not offset:
            for r in await self.store.legacy(actor):
                body.append([btn('Прежняя заявка № ' + r['id'], 'legacy:' + r['id'])])
        body += [[btn('📋 Действующие', 'list:0:0'), btn('🗂 История', 'list:1:0')],
                 [btn('👤 Мои данные', 'profile')],
                 [btn('📅 Забронировать', 'begin')]]
        text = ('🗂 История бронирований' if history else '📋 Мои бронирования') + \
               ('\n\nЗдесь пока пусто.' if not found else '\n\nВыберите бронирование.')
        return Screen(text, photo='booking', rows=rows(*body) + nav())

    async def show(self, actor, ident):
        r = await self.store.get(actor, ident)
        body = []
        if r['status'] in ('pending', 'confirmed') and r['paid'] < payment_total(r):
            if not r['paid']:
                body += [[btn('💳 Внести предоплату 2 000 ₽', 'payrequest:deposit:' + ident)],
                         [btn('💳 Оплатить полностью — ' + rub(payment_total(r)), 'payrequest:full:' + ident)]]
            else:
                body.append([btn('💳 Оплатить — ' + rub(payment_total(r) - r['paid']), 'payrequest:balance:' + ident)])
            requests = await self.store.payment_requests(actor, ident)
            linked = next((req for req in requests if req['url']), None)
            if linked:
                automatic = bool(await self.store.pool.fetchval(
                    'SELECT 1 FROM paykeeper_invoices WHERE request_id=$1', linked['id']))
                check_label = 'Проверить оплату ' + rub(linked['amount']) if automatic else '✅ Уже оплатил'
                check_action = ('paycheck:' if automatic else 'payreport:') + linked['id']
                if automatic:
                    index = 1 if linked['kind'] == 'full' and not r['paid'] else 0
                    body[index] = [btn(check_label, check_action)]
                    body.append([btn('🔗 Перейти к оплате', url=linked['url'])])
                else:
                    body += [[btn('🔗 Перейти к оплате', url=linked['url'])],
                             [btn(check_label, check_action)]]
        if self.settings.paykeeper_enabled:
            body.append([btn('Платежи и чеки', 'payhistory:' + ident)])
        if r['status'] in ('pending', 'confirmed'):
            body.append([btn('Отменить бронирование', 'cancelask:' + ident)])
        body += [[btn('☎️ Администратор', 'contact')],
                 [btn('← Назад', 'list:0:0' if r['status'] in ('pending', 'confirmed') else 'list:1:0')]]
        return Screen(card(r), photo='booking', rows=rows(*body) + nav())

    async def admin_link(self, actor):
        self.store.admin(actor)
        if not self.settings.web_url:
            return Screen('Панель пока недоступна.', rows=nav())
        token = await self.store.login_token(actor)
        return Screen('🔐 Панель администратора\n\nЛичная ссылка действует 5 минут.\nНе пересылайте её.',
                      rows=rows([btn('Открыть панель', url=self.settings.web_url + '/login#' + token)]) + nav())

    async def profile(self, actor):
        p = await self.store.profile(actor)
        text = ('👤 Мои данные\n\n' + p['name'] + '\n' + p['phone'] if p
                else '👤 Мои данные\n\nПока не сохранены.') + '\n\nТолько для будущих бронирований.'
        return Screen(text, rows=rows([btn('✏️ Изменить данные', 'profileedit')],
                                      [btn('← Назад', 'list:0:0')]) + nav())

    async def contact(self, actor):
        back = 'resume' if await self.unfinished(actor) else 'home'
        return Screen('☎️ Администратор\n\n' + self.settings.content['phone'] +
                      '\n\nПозвоните по этому номеру.\n'
                      'Для нестандартного времени согласуйте длительность заранее.',
                      rows=rows([btn('📍 Маршрут', url=self.settings.content['map_url'])],
                                [btn('← Назад', back)]) + nav())

    # ---------- text input ----------

    async def text(self, actor, message, contact_phone=''):
        async with self.locks[actor]:
            return await self._text(actor, message, contact_phone)

    async def _text(self, actor, message, contact_phone=''):
        text = (message or '').strip()
        try:
            if text in ('/start', '/menu', 'Главное меню', '🏠 Главное меню'):
                return await self.home(actor)
            if text == '/id':
                return Screen(str(actor), notice=str(actor))
            if text == '/admin':
                return await self.admin_link(actor)
            if text in ('/book', 'Забронировать'):
                return await self.start(actor)
            if text in ('/bookings', 'Мои бронирования'):
                return await self.listing(actor)
            if text in ('/help', 'Правила', 'Контакты', 'Цены', 'Цены и услуги', 'Помощь'):
                return await self.info(actor, 'Помощь' if text == '/help' else text)
            d = await self.unfinished(actor)
            if not d:
                return await self.home(actor)
            state, data = d['state'], dict(d['data'])
            if data.get('flow_version') != VERSION:
                return await self.render(actor, d)
            if '_filled' not in data:
                data['_filled'] = collected_fields(state, data)
            profile = state.startswith('profile_')
            if state in ('phone_contact', 'profile_contact') and text in (
                    '← Назад', '✕ Отменить оформление', '✖ Отменить оформление'):
                previous = 'profile_name' if profile else 'name'
                await self.store.save(actor, d, previous, data)
                return await self.render(actor, await self.store.draft(actor, d['token']))
            if state in ('phone', 'phone_contact', 'profile_phone', 'profile_contact'):
                if contact_phone:
                    phone = normalize_phone(contact_phone)
                elif text == '✓ Использовать ' + data.get('phone', ''):
                    phone = normalize_phone(data['phone'])
                else:
                    phone = normalize_phone(text)
                if profile:
                    await self.store.profile(actor, data['profile_name'], phone)
                    previous = data.pop('profile_return', 'name')
                    data.pop('profile_name', None)
                    await self.store.save(actor, d, previous, data)
                    return await self.profile(actor)
                data['phone'] = phone
                accept_field(data, 'phone')
                state = 'review' if data.pop('editing', False) and data.get('start') else 'guests'
            elif state in ('name', 'profile_name'):
                name = clean_name(text)
                if profile:
                    data['profile_name'] = name
                    state = 'profile_phone'
                else:
                    data['name'] = name
                    accept_field(data, 'name')
                    state = 'review' if data.pop('editing', False) and data.get('start') else 'phone'
            else:
                return Screen('На этом шаге выберите действие кнопкой.',
                              rows=rows([btn('Продолжить', 'resume')]) + nav())
            data['consent'] = False
            await self.store.save(actor, d, state, data)
            return await self.render(actor, await self.store.draft(actor, d['token']))
        except RuleError as error:
            return Screen(str(error), rows=rows([btn('Продолжить', 'resume')]) + nav())

    # ---------- button actions ----------

    async def action(self, actor, data):
        async with self.locks[actor]:
            return await self._action(actor, data or '')

    async def _action(self, actor, data):
        parts = data.split(':')
        action = parts[0]
        try:
            if action == 'home':
                return await self.home(actor)
            if action == 'begin':
                return await self.start(actor)
            if action == 'staffweb':
                return await self.admin_link(actor)
            if action == 'contact':
                return await self.contact(actor)
            if action == 'rulesfull':
                back = 'resume' if await self.unfinished(actor) else 'home'
                return Screen(self.settings.rules, rows=rows([btn('← Назад', back)]) + nav())
            if action == 'info':
                return await self.info(actor, parts[1])
            if action == 'list':
                return await self.listing(actor, bool(int(parts[1])), int(parts[2]))
            if action == 'open':
                return await self.show(actor, parts[1])
            if action == 'legacy':
                r = await self.store.legacy(actor, parts[1])
                if not r:
                    raise RuleError('Запись недоступна.')
                d = r['data']
                text = (f'Прежняя заявка № {r["id"]}\n{d.get("start", "")}\nГостей: {d.get("guests", "")}\n'
                        f'{d.get("name", "")}\n{d.get("phone", "")}\n\n'
                        'Архив прежней версии. Статус уточните у администратора.')
                return Screen(text, rows=rows([btn('← Назад', 'list:1:0')]) + nav())
            if action == 'cancelask':
                r = await self.store.get(actor, parts[1])
                if r['status'] == 'confirmed' or r['paid']:
                    await self.store.request_cancellation(actor, parts[1])
                    screen = await self.show(actor, parts[1])
                    return Screen(screen.text, screen.photo, screen.rows, screen.state,
                                  notice='Ваш запрос передан администратору.')
                if not can_cancel(r['status'], r['starts_at'], datetime.now(TZ)):
                    raise RuleError('Самостоятельная отмена недоступна. Свяжитесь с администратором.')
                return Screen('Отменить бронирование?\n\n' + card(r) +
                              '\n\nОсвободившееся время могут занять другие гости.',
                              rows=rows([btn('Да, отменить', 'cancel:' + parts[1])],
                                        [btn('← Назад', 'open:' + parts[1])]) + nav())
            if action == 'cancel':
                r = await self.store.get(actor, parts[1])
                if r['status'] == 'confirmed' or r['paid']:
                    await self.store.request_cancellation(actor, parts[1])
                    notice = 'Ваш запрос передан администратору.'
                else:
                    await self.store.transition(actor, parts[1], 'cancel')
                    notice = ''
                screen = await self.show(actor, parts[1])
                return Screen(screen.text, screen.photo, screen.rows, screen.state, notice=notice)
            if action == 'profile':
                return await self.profile(actor)
            if action == 'profileedit':
                token = await self.store.begin(actor)
                d = await self.store.draft(actor, token)
                if not d['state'].startswith('profile_'):
                    await self.store.save(actor, d, 'profile_name', dict(d['data'], profile_return=d['state']))
                return await self.render(actor, await self.store.draft(actor, token))
            if action == 'profilecancel':
                d = await self.store.draft(actor)
                if d['state'].startswith('profile_'):
                    data2 = dict(d['data'])
                    previous = data2.pop('profile_return', 'name')
                    data2.pop('profile_name', None)
                    await self.store.save(actor, d, previous, data2)
                screen = await self.profile(actor)
                return Screen(screen.text, screen.photo, screen.rows, screen.state,
                              notice='Изменение отменено.')
            if action == 'resume':
                d = await self.unfinished(actor)
                return await (self.render(actor, d) if d else self.start(actor))
            if action == 'payrequest':
                req, created = await self.store.request_payment(actor, parts[2], parts[1])
                screen = await self.show(actor, parts[2])
                if self.settings.paykeeper_enabled:
                    notice = ('Готовим ссылку на оплату. Она придёт отдельным сообщением.' if created
                              else 'Запрос уже сохранён. Используйте действующую ссылку или дождитесь её получения.')
                else:
                    notice = ('Запрос ссылки передан администратору.' if created
                              else 'Запрос уже передан администратору. Ожидайте ссылку.')
                return Screen(screen.text, screen.photo, screen.rows, screen.state, notice=notice)
            if action == 'paycheck':
                status = await self.store.check_paykeeper(actor, parts[1])
                ident = await self.store.pool.fetchval(
                    'SELECT booking_id FROM paykeeper_invoices WHERE request_id=$1', parts[1])
                from booking_domain import payment_confirmation
                current = await self.store.get(actor, ident)
                if status == 'paid':
                    notice = payment_confirmation(current)
                    if not current['paid']:
                        notice = ('Ваша оплата принята. Спасибо. Администратор проверит её привязку к брони. '
                                  'Повторно оплачивать не нужно.')
                elif status == 'expired':
                    notice = 'Срок счёта истёк. Получите новую ссылку; бронь сохранена.'
                else:
                    notice = 'Мы не видим вашей оплаты. Повторите позже.'
                screen = await self.show(actor, ident)
                return Screen(screen.text, screen.photo, screen.rows, screen.state, notice=notice)
            if action == 'payreport':
                ident, created = await self.store.report_payment(actor, parts[1])
                screen = await self.show(actor, ident)
                return Screen(screen.text, screen.photo, screen.rows, screen.state,
                              notice='Администратор получил ваше обращение и скоро ответит.')
            if action == 'payhistory':
                payments = await self.store.payment_history(actor, parts[1])
                text = 'Платежи и чеки'
                body = []
                for p in payments:
                    text += '\n\n' + p['created_at'].astimezone(TZ).strftime('%d.%m.%Y %H:%M') + ' · ' + rub(p['amount'])
                    if p['receipt_url']:
                        body.append([btn('Открыть чек · ' + rub(p['amount']), url=p['receipt_url'])])
                    else:
                        text += '\nЧек формируется.'
                    if p['disposition'] == 'review':
                        text += '\nПлатёж требует сверки администратором.'
                if not payments:
                    text += '\nПоступивших платежей пока нет.'
                final = await self.store.pool.fetchrow(
                    'SELECT state,url FROM paykeeper_settlements WHERE booking_id=$1', parts[1])
                if final:
                    text += '\n\nИтоговый чек на оказанную услугу. Повторная оплата не требуется.'
                    if final['state'] == 'success':
                        body.append([btn('Открыть итоговый чек', url=final['url'])])
                    else:
                        text += '\nЧек формируется или проверяется администратором.'
                return Screen(text, rows=rows(*body, [btn('← Назад', 'open:' + parts[1])]) + nav())
            if action == 'paystaff':
                raise RuleError('Решение по платежу принимается в веб-панели.')
            if len(parts) != 4:
                raise RuleError('Это кнопка прежнего экрана. Откройте текущее бронирование.')
            action, token, revision, value = parts
            if action == 'submit':
                ident, created = await self.store.submit(actor, token, expected_revision=int(revision),
                                                         accept_rules=True)
                text = ('✅ Заявка отправлена\n\nВремя занято за вами.\n'
                        'Для подтверждения внесите предоплату 2 000 ₽ или всю стоимость.\n'
                        'Откройте бронирование и запросите ссылку для оплаты.' if created
                        else 'Эта заявка уже отправлена.')
                return Screen(text, photo='booking',
                              rows=rows([btn('📋 Открыть бронирование', 'open:' + ident)]) + nav())
            d = await self.store.draft(actor, token)
            if d['revision'] != int(revision):
                return Screen('Экран уже изменился.\nПродолжите с актуального шага.',
                              rows=rows([btn('Продолжить', 'resume')]) + nav())
            state, data = d['state'], dict(d['data'])
            new = state
            if action in ('noop', 'limit'):
                if action == 'limit':
                    return Screen('Вместимость — до 8 гостей вместе с детьми.' if value == 'max'
                                  else 'Укажите хотя бы одного гостя.',
                                  rows=rows([btn('← Назад', 'resume')]) + nav())
                return Screen(nothing=True)
            if action in ('discardask', 'resetask', 'discard', 'reset') and state in ('phone_contact', 'profile_contact'):
                d = await self.store.draft(actor, token)
                data = dict(d['data'])
            if action == 'discardask':
                return Screen('Отменить оформление?\n\nОтправленные бронирования сохранятся.',
                              rows=rows([btn('Да, отменить оформление', callback('discard', d))],
                                        [btn('← Назад', 'resume')]) + nav())
            if action == 'discard':
                await self.store.abandon_draft(actor, d)
                return await self.home(actor)
            if action == 'resetask':
                return Screen('Начать заново?\n\nТолько незавершённый черновик будет сброшен. '
                              'Отправленные брони сохранятся.',
                              rows=rows([btn('Начать заново', callback('reset', d))],
                                        [btn('← Назад', 'resume')]) + nav())
            if action == 'reset':
                await self.store.reset_draft(actor, d)
                return await self.render(actor, await self.store.draft(actor))
            if action == 'resume_step':
                return await self.render(actor, d)
            if action == 'editmenu' and state == 'review':
                return Screen('✏️ Что изменить?\n\nОстальные данные сохранятся.', rows=rows(
                    [btn('👤 Имя', callback('edit', d, 'name')), btn('📱 Телефон', callback('edit', d, 'phone'))],
                    [btn('👥 Гости', callback('edit', d, 'guests'))],
                    [btn('📅 Дата', callback('edit', d, 'date')), btn('🕒 Время', callback('edit', d, 'slot'))],
                    [btn('← Назад', 'resume')]) + nav())
            if action == 'rules' and state == 'review':
                return await self.info(actor, 'Правила', back='resume')
            if action == 'share' and state in ('phone', 'profile_phone', 'phone_contact', 'profile_contact'):
                return await self.render(actor, d)
            if action in ('back', 'edit'):
                target = 'review' if action == 'back' and data.get('editing') and data.get('start') else value
                require(data, target)
                new = target
                if action == 'edit':
                    data['editing'] = True
                else:
                    data.pop('editing', None)
            elif action == 'keep' and ((value == 'name' and state == 'name') or (value == 'phone' and state == 'phone')):
                if value == 'name':
                    data['name'] = clean_name(data.get('name', ''))
                    accept_field(data, 'name')
                    new = 'phone'
                else:
                    data['phone'] = normalize_phone(data.get('phone', ''))
                    accept_field(data, 'phone')
                    new = 'guests'
                if data.pop('editing', False) and data.get('start'):
                    new = 'review'
            elif action == 'guest' and state == 'guests':
                data = change_guests(data, value[:-1], 1 if value.endswith('+') else -1)
            elif action == 'next' and state == 'guests' and value == 'date':
                accept_field(data, 'guests')
                require(data, 'date')
                new = 'review' if data.pop('editing', False) and data.get('start') else 'date'
            elif action == 'month' and state == 'date':
                month = date.fromisoformat(value)
                today = datetime.now(TZ).date()
                if not today.replace(day=1) <= month <= horizon(today).replace(day=1) or month.day != 1:
                    raise RuleError('Выберите доступный месяц.')
                data['month'] = value
            elif action == 'day' and state == 'date':
                day = date.fromisoformat(value)
                today = datetime.now(TZ).date()
                if not today <= day <= horizon(today):
                    raise RuleError('Выберите доступную дату.')
                if any(r['starts_at'].astimezone(TZ).date() == day for r in await self.store.listing(actor)):
                    raise RuleError('На эту дату у вас уже есть бронь.')
                if data.get('day') != value:
                    data.pop('start', None)
                data['day'] = value
                accept_field(data, 'day')
                new = 'slot'
            elif action == 'refresh' and state == 'slot':
                pass
            elif action == 'slot' and state == 'slot':
                hour = int(value)
                if hour not in SLOTS:
                    raise RuleError('Выберите время кнопкой.')
                start = datetime.combine(date.fromisoformat(data['day']), datetime.min.time(), TZ) + timedelta(hours=hour)
                validate_start(start, datetime.now(TZ))
                if not await self.store.free(start):
                    raise RuleError('Это время уже занято. Обновите список сеансов.')
                data['start'] = start.isoformat()
                accept_field(data, 'start')
                data.pop('editing', None)
                new = 'review'
            elif action == 'consent' and state == 'review':
                data['consent'] = not data.get('consent', False)
            else:
                raise RuleError('Кнопка недоступна на этом шаге. Продолжите бронирование.')
            if action != 'consent':
                data['consent'] = False
            await self.store.save(actor, d, new, data)
            return await self.render(actor, await self.store.draft(actor, token))
        except (RuleError, ValueError, IndexError, KeyError) as error:
            message = str(error) if isinstance(error, RuleError) else 'Откройте актуальный экран.'
            return Screen(message, rows=rows(
                [btn('Продолжить бронирование', 'resume')],
                [btn('📋 Мои бронирования', 'list:0:0')],
                [btn('☎️ Администратор', 'contact')]) + nav())

    async def dispatch(self, actor, action):
        """Route a transport action string to a screen."""
        action = (action or 'home').strip()
        if action.startswith('info:'):
            return await self.info(actor, action[5:])
        return await self.action(actor, action)
