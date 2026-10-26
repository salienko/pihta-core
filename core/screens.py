"""Transport-neutral screens for the guest journey.

The core builds text + button rows; a transport (Telegram, MAX) renders them
with its own keyboard API. No messenger imports are allowed in this module.

Button rows are lists of dicts:
    {"text": "label", "action": "begin"}      -> transport sends action back
    {"text": "label", "url": "https://..."}   -> transport opens the link

Action strings are the same tokens the Telegram build already uses, so the
existing callback grammar (``action:token:revision:value``) survives intact.
"""
from dataclasses import dataclass, field

from booking_domain import RuleError


@dataclass(frozen=True)
class Screen:
    text: str
    photo: str = ''
    rows: tuple = field(default_factory=tuple)
    state: str = ''
    reply_contact: bool = False   # transport must offer a contact request button

    def json(self):
        return {
            "text": self.text,
            "photo": self.photo or None,
            "rows": [list(row) for row in self.rows],
            "state": self.state or None,
            "reply_contact": self.reply_contact,
        }


def nav():
    return (({"text": "🏠 Главное меню", "action": "home"},),)


class Screens:
    """Builds screens for one actor. Read-only except where noted."""

    def __init__(self, settings, store):
        self.settings, self.store = settings, store

    async def unfinished(self, actor):
        try:
            return await self.store.draft(actor)
        except RuleError:
            return None

    async def home(self, actor):
        rows = [[{"text": "📅 Забронировать", "action": "begin"}],
                [{"text": "📋 Мои бронирования", "action": "list:0:0"}],
                [{"text": "💰 Цены", "action": "info:Цены"},
                 {"text": "📖 Правила", "action": "info:Правила"}],
                [{"text": "📍 Контакты", "action": "info:Контакты"},
                 {"text": "❓ Помощь", "action": "info:Помощь"}]]
        if await self.unfinished(actor):
            rows.insert(0, [{"text": "✏️ Продолжить бронирование", "action": "resume"}])
        if actor in self.settings.admin_ids:
            rows.append([{"text": "🔐 Панель администратора", "action": "staffweb"}])
        return Screen('ПИХТА · баня на дровах\n\nБаня целиком для вашей компании.\nВыберите действие.',
                      photo='home', rows=tuple(tuple(r) for r in rows))

    async def info(self, actor, section):
        c = self.settings.content
        rows = []
        if section == 'Контакты':
            text = (f'📍 Как нас найти\n\n{c["name"]}\nПосёлок Приладожское\n\n'
                    f'☎️ {c["phone"]}\nНестандартное время — по согласованию с администратором.')
            rows = [[{"text": "📍 Построить маршрут", "url": c['map_url']}]]
        elif section == 'Правила':
            text = ('📖 Правила бронирования\n\nДо 8 гостей вместе с детьми.\n'
                    'Дети до 7 лет включительно — без доплаты.\nПростыня на каждого гостя включена.\n\n'
                    'Для подтверждения внесите предоплату 2 000 ₽ или всю стоимость. Остаток — до начала сеанса.\n\n'
                    'Заявка занимает время и ждёт решения администратора.\n'
                    'Неоплаченную ожидающую заявку можно отменить самостоятельно. '
                    'Подтверждённую или оплаченную отменяет администратор по вашему запросу.\n\n'
                    'Отправленную бронь нельзя изменить: отмените её и создайте новую. Опоздание не продлевает сеанс.\n\n'
                    'Не более двух действующих броней на разные даты. Запись за час — два месяца до сеанса.')
            rows = [[{"text": "📄 Полные правила", "action": "rulesfull"}]]
        elif section in ('Цены', 'Цены и услуги'):
            text = ('💰 Аренда бани · 2 часа\n\nПн–пт до 19:00 — 5 000 ₽\n'
                    'С 19:00 и в выходные — 6 000 ₽\nПраздничные даты — 6 500 ₽*\n\n'
                    'Включены 4 гостя от 8 лет.\nЗа каждого следующего — 250 ₽/час.\n'
                    'Дети до 7 лет включительно — бесплатно.\n\n'
                    'Точная сумма будет перед отправкой.\n'
                    '*Особые даты и нестандартная длительность — через администратора.')
            rows = [[{"text": "📅 Забронировать", "action": "begin"}]]
        else:
            text = ('❓ Как записаться\n\nИмя → телефон → гости → дата → время.\n'
                    'Проверьте карточку и отправьте её администратору.\n\n'
                    'Черновик сохраняется: можно вернуться позже.\n'
                    'В «Моих бронированиях» — статус, детали и отмена.\n\n'
                    'Нужно другое время или помощь? Свяжитесь с администратором.')
            rows = [[{"text": "☎️ Администратор", "action": "contact"}]]
        rows.extend(nav())
        return Screen(text, photo='home', rows=tuple(tuple(r) for r in rows))

    async def dispatch(self, actor, action):
        """Route a transport button/text action to a screen."""
        action = (action or 'home').strip()
        if action.startswith('info:'):
            return await self.info(actor, action[5:])
        return await self.home(actor)
