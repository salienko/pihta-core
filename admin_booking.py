"""Telephone booking form, preview and atomic final submission."""
from datetime import date,datetime,timedelta
from aiohttp import web
from booking_domain import TZ,SLOTS,RuleError,estimate,rub,card
from domain import horizon


def mount_staff_booking(app,store,auth,form,page,e,navigation):
    def parse(data):
        try:
            start=datetime.fromisoformat(str(data.get('start','')))
            if start.tzinfo is None:start=start.replace(tzinfo=TZ)
            return dict(start=start,phone=str(data.get('phone','')),guests=int(data.get('guests','')),children=int(data.get('children','0')),name=str(data.get('name','')))
        except (ValueError,TypeError):raise RuleError('Проверьте дату и количество гостей.') from None

    async def new_booking(request):
        s=await auth(request);store.admin(s['actor'])
        try:day=date.fromisoformat(request.query.get('day',datetime.now(TZ).date().isoformat()))
        except ValueError:raise RuleError('Проверьте дату.') from None
        today=datetime.now(TZ).date()
        if not today<=day<=horizon(today):raise RuleError('Дата вне доступного периода бронирования.')
        body=navigation(s)+'<h1>Создать бронь по телефону</h1><div class="card"><form><label>Дата<input type="date" name="day" value="'+e(day.isoformat())+'" min="'+today.isoformat()+'" max="'+horizon(today).isoformat()+'" required></label><button>Показать свободные сеансы</button></form></div>'
        options=''
        catalog,holidays,rates=await store.pricing()
        for hour in SLOTS:
            start=datetime.combine(day,datetime.min.time(),TZ)+timedelta(hours=hour)
            try:free=await store.free(start)
            except RuleError:continue
            if not free:continue
            quote=estimate(dict(start=start.isoformat(),hours=2,guests=1,children=0,cart={}),catalog,holidays,rates)
            label=start.strftime('%H:%M')+'–'+(start+timedelta(hours=2)).strftime('%H:%M')+' · '+rub(quote['total'])+' для 1–4 платных гостей'
            options+='<option value="'+e(start.isoformat())+'">'+e(label)+'</option>'
        if options:
            fields='<label>Свободный сеанс<select name="start" required>'+options+'</select></label><label>Телефон гостя<input name="phone" type="tel" maxlength="32" required></label><label>Количество гостей, включая детей<input name="guests" type="number" min="1" max="8" value="1" required></label><label>Из них детей до 7 лет включительно<input name="children" type="number" min="0" max="8" value="0" required></label><label>Имя гостя (необязательно)<input name="name" maxlength="80"></label>'
            body+='<div class="card"><h2>Данные гостя</h2>'+form(s,'/new-booking/preview',fields,'Рассчитать и проверить')+'</div>'
        else:body+='<div class="notice">На эту дату свободных сеансов нет. Выберите другую дату.</div>'
        return web.Response(text=page('Новая бронь',body),content_type='text/html')

    async def preview(request):
        s=await auth(request,True);values=parse(await request.post())
        data,quote=await store.staff_quote(s['actor'],**values)
        if not await store.free(values['start']):raise RuleError('Этот сеанс уже занят. Выберите другое время.')
        row=dict(id='Предварительный расчёт',data=data,estimate=quote,starts_at=values['start'],ends_at=values['start']+timedelta(hours=2),status='pending',paid=0)
        fields=''.join('<input type="hidden" name="'+key+'" value="'+e(value.isoformat() if key=='start' else value)+'">' for key,value in values.items())
        fields+='<input type="hidden" name="expected_total" value="'+str(quote['total'])+'">'
        body=navigation(s)+'<h1>Проверьте новую бронь</h1><div class="card"><pre>'+e(card(row,personal=True))+'</pre><p>Время будет занято после создания брони. Сообщите гостю правила посещения.</p>'+form(s,'/new-booking/create',fields,'Создать бронь')+'<p><a href="/new-booking?day='+values['start'].date().isoformat()+'">← Назад</a></p></div>'
        return web.Response(text=page('Проверка брони',body),content_type='text/html')

    async def create(request):
        s=await auth(request,True);data=await request.post();values=parse(data)
        try:expected=int(data.get('expected_total',''))
        except (TypeError,ValueError):raise RuleError('Рассчитайте бронь заново.') from None
        ident,_=await store.create_staff_booking(s['actor'],str(data.get('operation','')),**values,expected_total=expected)
        raise web.HTTPFound('/booking/'+ident)

    app.add_routes([web.get('/new-booking',new_booking),web.post('/new-booking/preview',preview),web.post('/new-booking/create',create)])
