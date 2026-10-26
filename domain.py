"""Order intake rules. A request does not reserve the bath."""
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo
import calendar
import re

TZ = ZoneInfo('Europe/Moscow')
SLOTS = (10, 13, 16, 19, 22)


class RuleError(ValueError):
    pass


def now():
    return datetime.now(TZ)


def horizon(day):
    month = day.month + 2
    year = day.year + (month - 1) // 12
    month = (month - 1) % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def visit_end(start, hours, clock=None):
    clock = clock or now()
    if start.tzinfo is None or clock.tzinfo is None:
        raise RuleError('Дата должна содержать часовой пояс.')
    start = start.astimezone(TZ)
    if start.minute or start.second or start.microsecond or start.hour not in SLOTS:
        raise RuleError('Выберите один из предложенных сеансов.')
    if type(hours) is not int or hours not in (2, 3, 4, 5):
        raise RuleError('Продолжительность от 2 до 5 часов.')
    end = start + timedelta(hours=hours)
    if end > datetime.combine(start.date() + timedelta(days=1), time(), TZ):
        raise RuleError('Посещение должно закончиться не позднее 00:00.')
    if start < clock + timedelta(hours=1) or start.date() > horizon(clock.astimezone(TZ).date()):
        raise RuleError('Заявку можно подать минимум за час, на два месяца вперёд.')
    return end


def participants(guests, children):
    if type(guests) is not int or not 1 <= guests <= 8:
        raise RuleError('Введите общее число гостей от 1 до 8, включая детей.')
    if type(children) is not int or not 0 <= children <= guests:
        raise RuleError('Число детей до 6 лет включительно — от 0 до общего числа гостей.')


def normalize_phone(value):
    value = str(value).strip()
    if not re.fullmatch(r'\+?[0-9\s().-]+', value):
        raise RuleError('Введите номер +7XXXXXXXXXX, 8XXXXXXXXXX или 10 цифр.')
    digits = re.sub(r'[^0-9]', '', value)
    if value.startswith('+') and not (len(digits) == 11 and digits.startswith('7')):
        raise RuleError('Используйте российский номер с кодом +7.')
    if len(digits) == 10 and not value.startswith('+'):
        digits = '7' + digits
    elif len(digits) == 11 and digits.startswith('8'):
        digits = '7' + digits[1:]
    if len(digits) != 11 or not digits.startswith('7') or digits[1] == '0':
        raise RuleError('Проверьте номер: после +7 нужно 10 цифр.')
    return '+' + digits


def clean_name(value):
    value = ' '.join(str(value).split())
    if not 2 <= len(value) <= 100:
        raise RuleError('Укажите имя длиной от 2 до 100 символов.')
    return value


def rub(kopecks):
    whole, part = divmod(kopecks, 100)
    return f'{whole:,}'.replace(',', ' ') + (f',{part:02}' if part else '') + ' ₽'


def quote(data, catalog, holidays):
    """Integer kopecks; snapshots are rechecked before submission."""
    start = datetime.fromisoformat(data['start']).astimezone(TZ)
    hours, guests, children = data['hours'], data['guests'], data['children']
    participants(guests, children)
    special = holidays.get(start.date().isoformat())
    rent_lines = []
    for offset in range(hours):
        hour = start + timedelta(hours=offset)
        if special:
            base, extra = special['base'], special['extra']
        elif hour.weekday() >= 5 or hour.hour >= 19:
            base, extra = 600000, 280000
        else:
            base, extra = 500000, 230000
        rent_lines.append({'hour': hour.isoformat(), 'amount': base // 2 if offset < 2 else extra})
    guest_extra = max(0, guests - children - 4) * 25000 * hours
    items = []
    for code, quantity in sorted(data.get('cart', {}).items()):
        if code not in catalog or type(quantity) is not int or not 1 <= quantity <= 100:
            raise RuleError('Состав услуг изменился. Проверьте корзину.')
        item = catalog[code]
        items.append({'code': code, 'name': item['name'], 'quantity': quantity,
                      'unit_price': item['price'], 'total': item['price'] * quantity})
    rent = sum(line['amount'] for line in rent_lines) + guest_extra
    return {'rent': rent, 'rent_lines': rent_lines, 'guest_extra': guest_extra,
            'items': items, 'total': rent + sum(item['total'] for item in items),
            'holiday': special['label'] if special else None}


def order_card(ident, data, estimate, admin=False):
    start = datetime.fromisoformat(data['start']).astimezone(TZ)
    end = start + timedelta(hours=data['hours'])
    lines = [f'Заказ №{ident}', f'Желаемое посещение: {start:%d.%m.%Y %H:%M}–{end:%H:%M}',
             f'Продолжительность: {data["hours"]} ч.',
             f'Гостей: {data["guests"]}; детей до 6 включительно: {data["children"]}',
             f'Имя: {data["name"]}', f'Телефон: {data["phone"]}',
             f'Баня и гости: {rub(estimate["rent"])}']
    if estimate['holiday']:
        lines.append('Специальный тариф: ' + estimate['holiday'])
    lines.append('Услуги:')
    lines.extend(f'• {item["name"]} × {item["quantity"]} — {rub(item["total"])}' for item in estimate['items'])
    if not estimate['items']:
        lines.append('• Не выбраны')
    lines.extend([f'Предварительная стоимость: {rub(estimate["total"])}',
                  'Заявка не закрепляет время. Доступность времени и услуг уточнит администратор.'])
    if admin:
        lines.extend([f'Telegram ID: {data["telegram_id"]}',
                      'Профиль: @' + data['username'] if data.get('username') else 'Имя пользователя Telegram не задано.',
                      'Гость подтвердил ознакомление с правилами. Свяжитесь с ним для обработки заказа.'])
    return '\n'.join(lines)
