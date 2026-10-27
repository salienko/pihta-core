"""Booking policies. Money is integer kopecks; all dates are Moscow-aware."""
from datetime import datetime, timedelta
from domain import TZ, RuleError, horizon, participants, clean_name, normalize_phone, rub

SLOTS = (10, 13, 16, 19, 22)
ACTIVE = ('pending', 'confirmed')
LABELS = {'draft':'Черновик', 'pending':'Ждёт подтверждения', 'confirmed':'Подтверждено',
          'cancelled':'Отменено', 'expired':'Отменено автоматически',
          'rejected':'Отказано', 'completed':'Завершено'}

def validate_start(start, clock):
    if start.tzinfo is None or clock.tzinfo is None:
        raise RuleError('Нужен часовой пояс.')
    start, clock = start.astimezone(TZ), clock.astimezone(TZ)
    if start.hour not in SLOTS or start.minute or start.second or start.microsecond:
        raise RuleError('Выберите стандартный двухчасовой сеанс. Другое время — через администратора.')
    if start < clock + timedelta(hours=1) or start.date() > horizon(clock.date()):
        raise RuleError('Запись минимум за час и максимум на два календарных месяца.')
    return start + timedelta(hours=2)

def estimate(data, catalog, holidays):
    start = datetime.fromisoformat(data['start']).astimezone(TZ)
    if data.get('hours') != 2:
        raise RuleError('Нестандартное время согласовывается с администратором.')
    participants(data['guests'], data['children'])
    if start.month == 1 and start.day == 1 and start.date().isoformat() not in holidays:
        raise RuleError('Новогодний тариф уточните у администратора.')
    aroma = sum(data.get('cart', {}).get(code, 0) for code in ('eucalyptus','lavender'))
    if aroma > 1:
        raise RuleError('Выберите одно аромапарение: эвкалипт или лаванда.')
    rent = 0
    for offset in range(2):
        hour = start + timedelta(hours=offset)
        special = holidays.get(hour.date().isoformat())
        rent += (special['base'] if special else 600000 if hour.weekday() >= 5 or hour.hour >= 19 else 500000)//2
    extra = max(0, data['guests']-data['children']-4)*25000*2
    items=[]
    for code, qty in sorted(data.get('cart', {}).items()):
        if code not in catalog or type(qty) is not int or not 1 <= qty <= 100 or not catalog[code].get('available', True):
            raise RuleError('Проверьте доступность и количество услуг.')
        item=catalog[code]
        items.append(dict(code=code,name=item['name'],quantity=qty,unit_price=item['price'],total=item['price']*qty))
    return dict(rent=rent,guest_extra=extra,items=items,total=rent+extra+sum(i['total'] for i in items))

def payment_confirmation(row):
    text='Ваша оплата принята. Спасибо.'
    amount=row.get('actual_total') if row.get('actual_total') is not None else row['estimate']['total']
    if 0<row.get('paid',0)<amount and row['status'] in ('pending','confirmed'):
        text+=' Не забудьте внести оставшуюся сумму до начала сеанса.'
    return text


def can_cancel(status, start, clock):
    return status == 'pending'

def card(row, personal=True):
    d,q=row['data'],row['estimate']
    start=row['starts_at'].astimezone(TZ);end=row['ends_at'].astimezone(TZ)
    icons={'pending':'⏳','confirmed':'✅','cancelled':'✕','rejected':'✕','expired':'✕','completed':'✓','draft':'✏️'}
    min_age=d.get('guest_min_paid_age',7)
    due=(row.get('actual_total') if row.get('actual_total') is not None else q['total'])-row.get('paid',0)
    status_label='Бронь подтверждена, требуется доплата' if row['status']=='confirmed' and due>0 else LABELS[row['status']]
    lines=[f"Бронь № {row['id']}",f"{icons[row['status']]} {status_label}",'',f'📅 {start:%d.%m.%Y} · {start:%H:%M}–{end:%H:%M}',
           'Время московское','',f"Кол-во гостей: {d['guests']}",f"Гости от {min_age} лет: {d['guests']-d['children']}",
           f"Дети до {min_age-1} лет включительно: {d['children']}",f"Простыни включены: {d['guests']} шт.",'',f"Аренда: {rub(q['rent'])}"]
    if q['guest_extra']:lines.append(f"Доплата за гостей: {rub(q['guest_extra'])}")
    # Historical snapshots can contain old goods; display them without enabling orders.
    lines += [f"{i['name']} × {i['quantity']} — {rub(i['total'])}" for i in q['items']]
    lines += [f"Итого по брони: {rub(q['total'])}"]
    actual=row.get('actual_total');total=actual if actual is not None else q['total']
    if actual is not None:lines.append('Фактический расчёт: '+rub(actual))
    paid=row.get('paid',0)
    if paid>=total:lines.append('✅ Внесена вся стоимость')
    elif paid>=200000:lines.append('✅ Внесена предоплата')
    if paid:lines.append('Оплачено: '+rub(paid))
    if row['status'] in ('cancelled','rejected','expired'):
        if paid:lines.append('Расчёт и возможный возврат — через администратора.')
    else:
        lines.append(('К оплате: ' if not paid else 'Остаток: ')+rub(max(0,total-paid)))
        if paid>total:lines.append('Переплата: '+rub(paid-total)+' · обратитесь к администратору.')
    if row.get('reason'):lines+=['','Причина: '+row['reason']]
    if row.get('no_show'):lines+=['','Администратор отметил неявку.']
    if row['status']=='confirmed' and start<=datetime.now(TZ)<end:lines+=['','Сеанс идёт. Окончание по брони.']
    phone=row.get('contact_phone') or d['phone']
    if personal:lines += ['',f"👤 {d['name']}",f"📱 {phone}"]
    if row['status'] in ('pending','confirmed'):
        if not paid:
            lines += ['','Для подтверждения бронирования необходимо внести предоплату 2 000 ₽ либо оплатить всю сумму сразу.','Получите ссылку для оплаты ↓']
        elif paid<total:
            lines += ['','Осталось доплатить: '+rub(total-paid),'Получите ссылку для оплаты ↓']
    return '\n'.join(lines)
