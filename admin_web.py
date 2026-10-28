"""Server-rendered admin panel. Telegram-issued, single-use authentication."""
from datetime import datetime,timedelta
from decimal import Decimal,InvalidOperation
import hashlib
import hmac
from html import escape
import os
import secrets
from aiohttp import web
from booking_domain import TZ,RuleError,LABELS,card,rub

STYLE='''*{box-sizing:border-box}body{margin:0;background:#eff3ee;color:#213b2d;font:16px system-ui,sans-serif}header{background:#203d2e;color:white;padding:22px max(5vw,18px)}main{max-width:1100px;margin:auto;padding:24px 18px}h1{font-size:28px}h2{font-size:21px}a{color:#246448}header a{color:white;margin-right:20px}.card{padding:22px;background:white;border-radius:16px;margin:16px 0;box-shadow:0 2px 7px #1a39200b}pre{white-space:pre-wrap;font:inherit;line-height:1.65}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px}input,select,textarea,button{font:inherit;padding:12px;border:1px solid #cbd6cc;border-radius:9px;max-width:100%}label{display:block;margin:12px 0}input,select,textarea{display:block;width:100%;margin-top:5px}button{background:#2c6145;color:white;cursor:pointer}button.danger{background:#864735}small,.muted{color:#617568}.badge{display:inline-block;background:#e7efe7;padding:5px 10px;border-radius:20px}table{width:100%;border-collapse:collapse}td,th{padding:12px 8px;text-align:left;border-bottom:1px solid #e4ebe4}.scroll{overflow:auto}nav{display:flex;gap:20px;flex-wrap:wrap}.notice{padding:16px;background:#fff4d6;border-radius:12px}.inline{display:inline}header form button{padding:6px 15px;background:transparent}.button{display:inline-block;background:#2c6145;color:white;text-decoration:none;padding:12px;border-radius:9px}'''
def e(v):return escape(str(v),quote=True)
def release_label() -> str:
    return os.getenv('RELEASE') or os.getenv('CORE_RELEASE') or 'dev'
def page(title,body):return '<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+e(title)+' · Пихта</title><link rel="stylesheet" href="/style.css"><header><strong>ПИХТА · Управление</strong></header><main>'+body+'</main></html>'
def money(value):
    try:
        amount=Decimal(value)*100
        if amount!=amount.to_integral_value() or not amount.is_finite() or amount<0 or amount>100000000:raise ValueError()
        return int(amount)
    except (InvalidOperation,ValueError,TypeError):raise RuleError('Введите сумму в рублях, не более двух знаков после точки.')

ACTION_NAMES={'staff_submitted':'Создана телефонная бронь','reserve_cancelled':'Бронь отменена по сроку оплаты','paykeeper_invoice_requested':'Запрошен счёт на оплату','staff_invoice_requested':'Сотрудник запросил счёт','paykeeper_payment':'Получен платёж','settlement_requested':'Подтверждено оказание услуги, запрошен итоговый чек','settlement_success':'Итоговый чек сформирован','settlement_failed':'Ошибка итогового чека','settlement_reconciled':'Итоговый чек привязан после сверки'}

def make_app(store,settings,secure=True):
    @web.middleware
    async def security(request,handler):
        try:response=await handler(request)
        except RuleError as error:response=web.Response(status=400,text=page('Не выполнено','<div class="card"><h1>Действие не выполнено</h1><p>'+e(error)+'</p><a href="/">Вернуться</a></div>'),content_type='text/html')
        except web.HTTPException as error:response=error
        response.headers.update({'Cache-Control':'no-store','Referrer-Policy':'same-origin','X-Content-Type-Options':'nosniff','X-Frame-Options':'DENY','Content-Security-Policy':"default-src 'none'; style-src 'self'; script-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'; connect-src 'self'; img-src 'self'"})
        if secure:response.headers['Strict-Transport-Security']='max-age=31536000'
        return response
    app=web.Application(middlewares=[security],client_max_size=16384)

    async def auth(request,post=False):
        s=await store.session(request.cookies.get('pihta_session'))
        if not s:raise web.HTTPFound('/login')
        if post:
            if request.headers.get('Origin')!=settings.web_url:raise web.HTTPForbidden(text='Неверный источник запроса.')
            data=await request.post()
            if not hmac.compare_digest(str(data.get('csrf','')),s['csrf']):raise web.HTTPForbidden(text='Обновите страницу и повторите действие.')
        return s
    def form(s,action,fields,label='Сохранить',danger=False):
        return f'<form method="post" action="{e(action)}"><input type="hidden" name="csrf" value="{e(s["csrf"])}"><input type="hidden" name="operation" value="{secrets.token_hex(16)}">'+fields+f'<button class="{"danger" if danger else ""}">{e(label)}</button></form>'
    def navigation(s):return '<nav><a href="/">Заявки и календарь</a><a href="/new-booking">Создать бронь</a><a href="/catalog">Тарифы и праздники</a><a href="/archive">Прежние заявки</a><a href="/audit">Журнал действий</a>'+('<a href="/payments">Счета и чеки</a>' if settings.paykeeper_enabled else '')+'</nav>'+form(s,'/logout','','Выйти')

    async def login_page(request):
        body='<div class="card"><h1>Вход администратора</h1><p>Получите личную одноразовую ссылку командой /admin в боте «Пихта».</p><form id="login" action="/login" method="post"><input type="hidden" id="token" name="token"><button id="enter" hidden>Войти</button></form><script src="/login.js" defer></script></div>'
        return web.Response(text=page('Вход',body),content_type='text/html')
    async def login_js(request):return web.Response(text="function takeToken(){const token=location.hash.slice(1);history.replaceState(null,'','/login');if(token){document.getElementById('token').value=token;document.getElementById('enter').hidden=false;}}takeToken();addEventListener('hashchange',takeToken);",content_type='text/javascript')
    async def login(request):
        if request.headers.get('Origin')!=settings.web_url:raise web.HTTPForbidden()
        data=await request.post();token=await store.login(str(data.get('token','')))
        response=web.HTTPFound('/');response.set_cookie('pihta_session',token,secure=secure,httponly=True,samesite='Strict',max_age=8*3600,path='/');return response
    async def logout(request):
        await auth(request,True);token=request.cookies['pihta_session']
        await store.pool.execute('DELETE FROM web_sessions WHERE hash=$1',hashlib.sha256(token.encode()).hexdigest())
        r=web.HTTPFound('/login');r.del_cookie('pihta_session');return r

    async def home(request):
        s=await auth(request);offset=max(0,int(request.query.get('offset','0')));status_filter=request.query.get('status','pending');status_filter=None if status_filter=='all' else status_filter;rows=await store.listing(s['actor'],staff=True,offset=offset,status_filter=status_filter)
        day=date_value=request.query.get('day',datetime.now(TZ).date().isoformat())
        start=datetime.fromisoformat(day).replace(tzinfo=TZ);end=start+timedelta(days=1)
        calendar_rows=await store.pool.fetch("SELECT * FROM bookings WHERE starts_at<$2 AND blocked_until>$1 AND status IN ('pending','confirmed','completed','expired') ORDER BY starts_at",start,end)
        body=navigation(s)+'<h1>Бронирования</h1><p>Подтвердите аренду или откажите с причиной. Отправленная бронь не редактируется.</p>'
        from booking_store import unpack
        body+='<div class="card"><h2>Календарь</h2><form><label>Дата<input type="date" name="day" value="'+e(day)+'"></label><button>Показать</button></form>'
        for raw in calendar_rows:
            r=unpack(raw);body+=f'<p><a href="/booking/{e(r["id"])}">{r["starts_at"].astimezone(TZ):%H:%M}–{r["ends_at"].astimezone(TZ):%H:%M} · {e(LABELS[r["status"]])}</a> · уборка до {r["blocked_until"].astimezone(TZ):%H:%M}</p>'
        if not calendar_rows:body+='<p>Броней на дату нет.</p>'
        body+='</div><div class="card"><h2>Список заявок</h2><nav>'+''.join('<a href="/?status='+key+'">'+label+'</a>' for key,label in [('pending','Ждут решения'),('confirmed','Подтверждены'),('all','Все записи')])+'</nav></div><div class="card scroll"><table><tr><th>Дата</th><th>Бронь</th><th>Статус</th><th>Итог</th></tr>'
        for r in rows[:20]:body+=f'<tr><td>{r["starts_at"].astimezone(TZ):%d.%m.%Y %H:%M}</td><td><a href="/booking/{e(r["id"])}">{e(r["id"])}</a></td><td>{e(LABELS[r["status"]])}</td><td>{e(rub(r["estimate"]["total"]))}</td></tr>'
        body+='</table></div>'
        status_query=e(status_filter or 'all')
        if offset:body+=f'<a href="/?status={status_query}&amp;offset={max(0,offset-20)}">← Назад</a> '
        if len(rows)>20:body+=f'<a href="/?status={status_query}&amp;offset={offset+20}">Далее →</a>'
        errors=await store.pool.fetchval('SELECT count(*) FROM outbox WHERE delivered_at IS NULL AND last_error IS NOT NULL')
        body+=f'<p>Уведомлений с ошибками доставки: {errors}. В Telegram необходимо открыть /start для получения сообщений.</p>'
        body+='<div class="card"><h2>Закрыть время для записи</h2>'+form(s,'/block','<label>Начало<input name="start" type="datetime-local" required></label><label>Окончание, включая уборку<input name="end" type="datetime-local" required></label><label>Причина<input name="reason" minlength="2" maxlength="500" required></label>','Закрыть интервал')+'</div>'
        blocks=await store.pool.fetch('SELECT * FROM calendar_blocks WHERE active ORDER BY starts_at DESC LIMIT 50')
        for b in blocks:body+='<div class="card">'+e(str(b['starts_at'].astimezone(TZ)))+' — '+e(str(b['ends_at'].astimezone(TZ)))+'<p>'+e(b['reason'])+'</p>'+form(s,'/unblock/'+str(b['id']),'','Открыть интервал')+'</div>'
        return web.Response(text=page('Календарь',body),content_type='text/html')

    async def detail(request):
        s=await auth(request);r=await store.get(s['actor'],request.match_info['id'],staff=True)
        body=navigation(s)+'<h1>Бронь № '+e(r['id'])+'</h1><div class="card"><pre>'+e(card(r))+'</pre></div>'
        if r['telegram_id'] is None:body+='<div class="notice">Бронь создана по телефону. Ссылку на оплату скопируйте и передайте гостю. Автоматические сообщения Telegram и отмена по таймеру доставки ссылки для этой брони не применяются.</div>'
        if r['status']=='pending':body+='<div class="notice">Бронь подтвердится автоматически после фиксации поступления минимум 2 000 ₽ или всей стоимости. Ссылка и сообщение гостя «Уже оплатил» не являются подтверждением поступления.</div>'
        if r['status'] in ('pending','confirmed'):
            body+='<div class="card">'+form(s,'/booking/'+r['id']+'/cancel','<label>Причина отмены<textarea name="reason" minlength="2" maxlength="500" required></textarea></label>','Отменить с уведомлением',True)+'</div>'
            if r['status']=='pending':body+='<div class="card">'+form(s,'/booking/'+r['id']+'/reject','<label>Причина отказа<textarea name="reason" minlength="2" maxlength="500" required></textarea></label>','Отказать',True)+'</div>'
        body+='<div class="grid">'
        for action,title in [('payment','Подтвердить поступление денег'),('refund','Фактически возвращено вручную')]:
            body+='<div class="card"><h2>'+title+'</h2>'+form(s,'/booking/'+r['id']+'/'+action,'<label>Сумма, ₽<input name="amount" type="number" min="0.01" max="1000000" step="0.01" required></label><label>Комментарий<input name="note" minlength="2" maxlength="500" required></label>','Зафиксировать факт')+'</div>'
        body+='</div><div class="card"><h2>Фактическое посещение — отдельно от брони</h2><p>Исходная сумма и состав сохраняются. Меньшее число гостей не снижает стоимость.</p>'+form(s,'/booking/'+r['id']+'/actual','<label>Фактических гостей<input name="guests" type="number" min="1" max="8" required></label><label>Итог фактического расчёта, ₽<input name="total" type="number" min="0" step="0.01" required></label><label>Обоснование расчёта<textarea name="note" minlength="2" maxlength="500" required></textarea></label>')+'</div>'
        if r['telegram_id'] is not None:body+='<div class="card"><h2>Связаться с гостем</h2><p>Уточните детали посещения. Сообщение не меняет бронь и занятость.</p>'+form(s,'/booking/'+r['id']+'/message','<label>Сообщение<textarea name="text" minlength="2" maxlength="1000" required></textarea></label>','Отправить гостю')+'</div>'
        if r['status'] in ('confirmed','completed'):body+='<div class="card">'+form(s,'/booking/'+r['id']+'/no_show','<label>Комментарий<input name="note" minlength="2" maxlength="500" required></label>','Отметить неявку')+'</div>'
        events=await store.pool.fetch('SELECT * FROM audit WHERE booking_id=$1 ORDER BY id DESC LIMIT 100',r['id'])
        body+='<div class="card"><h2>История действий</h2>'
        for event in events:body+='<p>'+e(str(event['created_at'].astimezone(TZ)))+' · '+e(ACTION_NAMES.get(event['action'],event['action']))+' · сотрудник '+e(event['actor'])+'</p>'
        body+='</div>'
        if settings.paykeeper_enabled:
            body+='<div class="card"><h2>Платежи и чеки</h2><a href="/payments?booking='+e(r['id'])+'">Выставить счёт по этой брони</a>'
            for p in await store.payment_history(s['actor'],r['id'],staff=True):
                body+='<p>'+e(rub(p['amount']))+' · '+e({'applied':'Учтён','review':'Требует сверки','standalone':'Отдельный платёж'}[p['disposition']])
                if p['receipt_url']:body+=' · <a href="'+e(p['receipt_url'])+'" target="_blank" rel="noreferrer">Открыть чек</a>'
                else:body+=' · Чек формируется'
                body+='</p>'
            body+='</div>'
            if settings.paykeeper_settlement_enabled:
                final=await store.pool.fetchrow('SELECT * FROM paykeeper_settlements WHERE booking_id=$1',r['id'])
                body+='<div class="card"><h2>Итоговый чек на оказанную услугу</h2>'
                if final:
                    labels={'queued':'Ожидает отправки','sending':'Отправляется','pending':'Касса формирует чек','uncertain':'Нужна сверка — не выдавайте повторно','success':'Выдан','failed':'Ошибка кассы — нужна проверка'}
                    body+='<p>'+e(labels[final['state']])+' · '+e(rub(final['amount']))+'</p>'
                    if final['url']:body+='<a href="'+e(final['url'])+'" target="_blank" rel="noreferrer">Открыть итоговый чек</a>'
                    if final['state']=='uncertain':
                        body+='<p>Ключ для сверки: '+e(final['receipt_key'])+'</p>'+form(s,'/booking/'+r['id']+'/settlement_reconcile','<label>Номер уже созданного чека<input name="receipt_id" pattern="[0-9]{1,40}" required></label>','Проверить и привязать чек')
                else:
                    from booking_payments import total
                    body+='<p>Стоимость услуги: '+e(rub(total(r)))+'. Ранее внесено: '+e(rub(r['paid']))+'. Новый платёж не создаётся. Действие отправит запрос в кассу.</p>'+form(s,'/booking/'+r['id']+'/settlement','<label><input type="checkbox" name="service_done" value="yes" required>Подтверждаю: услуга фактически оказана, сумма и платежи проверены.</label>','Услуга оказана — оформить итоговый чек')
                body+='</div>'
        return web.Response(text=page('Бронь',body),content_type='text/html')

    async def action(request):
        s=await auth(request,True);data=await request.post();ident=request.match_info['id'];kind=request.match_info['action']
        if kind=='settlement':
            if data.get('service_done')!='yes':raise RuleError('Подтвердите фактическое оказание услуги.')
            await store.request_final_receipt(s['actor'],ident)
        elif kind=='settlement_reconcile':await store.reconcile_settlement(s['actor'],ident,str(data.get('receipt_id','')))
        elif kind in ('confirm','reject','cancel'):await store.transition(s['actor'],ident,kind,str(data.get('reason','')),staff=True)
        elif kind=='message':
            key=str(data.get('operation',''))
            if not re_full_token(key):raise RuleError('Обновите форму.')
            await store.staff_note(s['actor'],ident,key,str(data.get('text','')))
        else:
            key=str(data.get('operation',''))
            if not re_full_token(key):raise RuleError('Обновите форму.')
            await store.accounting(s['actor'],ident,key,kind,amount=money(data.get('amount','0')),guests=int(data['guests']) if data.get('guests') else None,total=money(data['total']) if data.get('total') else None,note=str(data.get('note','')))
        raise web.HTTPFound('/booking/'+ident)

    async def block(request):
        s=await auth(request,True);data=await request.post();start=datetime.fromisoformat(data['start']).replace(tzinfo=TZ);end=datetime.fromisoformat(data['end']).replace(tzinfo=TZ);reason=str(data.get('reason','')).strip()
        if not start<end or end-start>timedelta(days=90) or not 2<=len(reason)<=500:raise RuleError('Проверьте интервал и причину.')
        async with store.tx() as db:
            if await store.blocked(db,start,end):raise RuleError('В интервале есть бронь или блокировка. Сначала проверьте календарь.')
            await db.execute('INSERT INTO calendar_blocks(starts_at,ends_at,reason,actor) VALUES($1,$2,$3,$4)',start,end,reason,s['actor']);await store.audit(db,None,s['actor'],'block',dict(start=start.isoformat(),end=end.isoformat(),reason=reason))
        raise web.HTTPFound('/')
    async def unblock(request):
        s=await auth(request,True)
        async with store.tx() as db:
            await db.execute('UPDATE calendar_blocks SET active=false WHERE id=$1',int(request.match_info['id']));await store.audit(db,None,s['actor'],'unblock',{'id':request.match_info['id']})
        raise web.HTTPFound('/')
    async def archive(request):
        s=await auth(request);body=navigation(s)+'<h1>Прежние заявки</h1><p>Заявки старой версии не меняются и сами по себе не бронировали время. Проверьте их вручную перед началом работы с календарём.</p>'
        exists=await store.pool.fetchval("SELECT to_regclass('pihta_intake_v1.orders') IS NOT NULL")
        if exists:
            rows=await store.pool.fetch('SELECT id,data,created_at FROM pihta_intake_v1.orders ORDER BY created_at DESC LIMIT 200')
            import json
            for r in rows:
                d=json.loads(r['data']) if isinstance(r['data'],str) else r['data']
                body+='<div class="card"><h2>'+e(r['id'])+'</h2><pre>'+e('\n'.join(f'{k}: {v}' for k,v in d.items() if k in ('start','hours','guests','children','name','phone','cart')))+'</pre></div>'
        else:body+='<p>Прежних заявок нет.</p>'
        return web.Response(text=page('Архив',body),content_type='text/html')
    async def audit(request):
        s=await auth(request);rows=await store.pool.fetch('SELECT * FROM audit ORDER BY id DESC LIMIT 100');body=navigation(s)+'<h1>Журнал действий</h1>'
        for r in rows:body+='<div class="card">'+e(str(r['created_at'].astimezone(TZ)))+' · '+e(ACTION_NAMES.get(r['action'],r['action']))+' · '+e(r['actor'])+'<pre>'+e(r['detail'])+'</pre></div>'
        return web.Response(text=page('Журнал',body),content_type='text/html')
    async def health(request):
        try:await store.pool.fetchval('SELECT 1')
        except Exception:return web.json_response({'status':'unavailable'},status=503)
        return web.json_response({'status':'ok','release':release_label()})
    async def catalog(request):
        s=await auth(request);items,holidays=await store.pricing();body=navigation(s)+'<h1>Тарифы и праздничные даты</h1><p>Пн–пт до 19:00: 5 000 ₽ за 2 часа. Вечером и в выходные: 6 000 ₽. Ниже задаются особые даты. Изменения относятся только к новым бронированиям.</p>'
        body+='<div class="card"><h2>Праздничный тариф</h2><p>Укажите каждую дату отдельно. Новогодняя цена задаётся явно.</p>'+form(s,'/holiday','<label>Дата<input name="day" type="date" required></label><label>Название<input name="label" maxlength="80" required></label><label>Первые 2 часа, ₽<input name="base" type="number" min="1" step="1" value="6500" required></label><label>Следующий час, ₽<input name="extra" type="number" min="1" step="1" value="3000" required></label>','Сохранить тариф')+'</div>'
        for day,rate in sorted(holidays.items()):body+='<div class="card"><p>'+e(day)+' · '+e(rate['label'])+' · '+e(rub(rate['base']))+' / '+e(rub(rate['extra']))+'</p>'+form(s,'/holiday/remove','<input type="hidden" name="day" value="'+e(day)+'">','Убрать особый тариф')+'</div>'
        return web.Response(text=page('Каталог',body),content_type='text/html')
    async def holiday(request):
        s=await auth(request,True);data=await request.post()
        from datetime import date
        day=date.fromisoformat(data['day']);label=str(data.get('label','')).strip();base=money(data.get('base'));extra=money(data.get('extra'))
        if not 1<=len(label)<=80 or base<=0 or base%2 or extra<=0:raise RuleError('Проверьте тариф.')
        async with store.tx() as db:
            await db.execute('INSERT INTO holiday_rates VALUES($1,$2,$3,$4) ON CONFLICT(day) DO UPDATE SET label=$2,base=$3,extra=$4',day,label,base,extra);await store.audit(db,None,s['actor'],'holiday',dict(day=day.isoformat(),label=label,base=base,extra=extra))
        raise web.HTTPFound('/catalog')
    async def remove_holiday(request):
        s=await auth(request,True);data=await request.post()
        from datetime import date
        day=date.fromisoformat(data['day'])
        async with store.tx() as db:
            await db.execute('DELETE FROM holiday_rates WHERE day=$1',day)
            await store.audit(db,None,s['actor'],'holiday_removed',dict(day=day.isoformat()))
        raise web.HTTPFound('/catalog')

    async def payments(request):
        s=await auth(request);store.admin(s['actor'])
        if not settings.paykeeper_enabled:raise web.HTTPNotFound()
        booking=request.query.get('booking','');phone='';name='';amount=''
        if booking:
            from booking_payments import total
            selected=await store.get(s['actor'],booking,staff=True)
            phone=selected['data']['phone'];name=selected['data']['name']
            due=max(0,total(selected)-selected['paid']);amount=f'{due//100}.{due%100:02}' if due else ''
        fields=(('<label>Имя гостя<input readonly value="'+e(name)+'"></label>' if booking else '')+
                '<label>Телефон клиента<input name="phone" type="tel" maxlength="32" value="'+e(phone)+'" '+('readonly ' if booking else '')+'required></label>'
                '<label>Сумма, ₽<input name="amount" type="number" min="0.01" max="1000000" step="0.01" value="'+e(amount)+'" required></label>'
                '<label>E-mail для чека (по желанию)<input name="email" type="email" maxlength="254"></label>'
                '<label>Номер брони (необязательно)<input name="booking" maxlength="32" value="'+e(booking)+'" '+('readonly' if booking else '')+'></label>'
                '<label>Способ расчёта<select name="payment_type" required><option value="">Выберите согласованный способ</option>'
                '<option value="part_prepay">Частичная предоплата</option><option value="prepay">Предоплата 100%</option>'
                '<option value="advance">Аванс</option><option value="full">Полный расчёт</option></select></label>')
        body=navigation(s)+'<h1>Счета и чеки</h1><div class="card"><h2>Выставить счёт</h2><p>Позиция: Оплата аренды бани «Пихта».</p>'+form(s,'/payments',fields,'Создать счёт')+'</div><a href="/payments">Обновить статусы</a>'
        labels={'queued':'Готовится','creating':'Готовится','uncertain':'Нужна сверка','ready':'Ожидает оплаты','received':'Оплата получена','expired':'Счёт закрыт'}
        for i in await store.pool.fetch('SELECT * FROM paykeeper_invoices ORDER BY created_at DESC LIMIT 50'):
            body+='<div class="card"><strong>'+e(rub(i['amount']))+'</strong> · '+e(labels[i['state']])+'<p>Телефон: '+e(i['phone'])+'</p><p>Запрос: '+e(i['request_id'])+'</p>'
            if i['booking_id']:body+='<a href="/booking/'+e(i['booking_id'])+'">Открыть бронь</a>'
            if i['url']:
                body+='<p><a href="'+e(i['url'])+'" target="_blank" rel="noreferrer">Открыть счёт</a></p><label>Ссылка<input readonly value="'+e(i['url'])+'"></label><button type="button" data-copy="'+e(i['url'])+'">Скопировать ссылку</button><span role="status"></span>'
            if i['state']=='uncertain':body+='<p>Проверьте в платёжном сервисе счёт с этим номером запроса. Повторное создание заблокировано.</p>'+form(s,'/payments/'+i['request_id']+'/resolve','<label>Номер найденного счёта<input name="invoice_id" maxlength="40" required></label>','Проверить и привязать счёт')
            for r in await store.pool.fetch('SELECT r.* FROM paykeeper_fiscal_receipts r JOIN paykeeper_payments p USING(payment_id) WHERE p.request_id=$1',i['request_id']):
                body+='<p><a href="'+e(r['url'])+'" target="_blank" rel="noreferrer">Открыть чек</a></p>'
            body+='</div>'
        return web.Response(text=page('Счета',body+'<script src="/payments.js" defer></script>'),content_type='text/html')

    async def payment_create(request):
        s=await auth(request,True);data=await request.post()
        await store.create_staff_invoice(s['actor'],str(data.get('operation','')),str(data.get('phone','')),money(data.get('amount','0')),
                                         str(data.get('payment_type','')),str(data.get('email','')),str(data.get('booking','')).strip() or None)
        raise web.HTTPFound('/payments')

    async def payment_resolve(request):
        s=await auth(request,True);data=await request.post()
        await store.resolve_invoice(s['actor'],request.match_info['id'],str(data.get('invoice_id','')))
        raise web.HTTPFound('/payments')

    async def payment_js(request):
        return web.Response(text="document.addEventListener('click',async e=>{const b=e.target.closest('[data-copy]');if(!b)return;const s=b.nextElementSibling;try{await navigator.clipboard.writeText(b.dataset.copy);s.textContent=' Ссылка скопирована';}catch{const i=b.previousElementSibling.querySelector('input');i.focus();i.select();s.textContent=' Скопируйте выделенную ссылку';}});",content_type='text/javascript')

    async def style(request):return web.Response(text=STYLE,content_type='text/css')
    from paykeeper import mount
    mount(app,store,settings)
    from admin_booking import mount_staff_booking
    mount_staff_booking(app,store,auth,form,page,e,navigation)
    app.add_routes([web.get('/payments',payments),web.post('/payments',payment_create),web.post('/payments/{id}/resolve',payment_resolve),web.get('/payments.js',payment_js)])
    app.add_routes([web.get('/style.css',style),web.get('/health',health),web.get('/login',login_page),web.get('/login.js',login_js),web.post('/login',login),web.post('/logout',logout),web.get('/',home),web.get('/booking/{id}',detail),web.post('/booking/{id}/{action}',action),web.post('/block',block),web.post('/unblock/{id}',unblock),web.get('/archive',archive),web.get('/audit',audit),web.get('/catalog',catalog),web.post('/holiday',holiday),web.post('/holiday/remove',remove_holiday)])
    return app

def re_full_token(value):
    import re
    return bool(re.fullmatch('[a-f0-9]{32}',value))
