"""Persistent draft cart and administrator-approved duration requests."""
import json
from datetime import datetime, timedelta
from uuid import uuid4
from domain import RuleError, window, now


def uid() -> str:
    return uuid4().hex


class CheckoutStore:
    def __init__(self, store):
        self.s = store

    async def start(self, actor, data, booking=None):
        if booking:
            b = await self.s.get(booking, actor)
            if b['guest_id'] != actor or b['status'] not in ('held','payment_review','review_overdue','confirmed','in_progress'):
                raise RuleError('Этот заказ закрыт или принадлежит другому гостю.')
            data = {'booking':booking, 'start':b['start_at'].astimezone(now().tzinfo).isoformat(),
                    'hours':int((b['end_at']-b['start_at']).total_seconds()/3600),
                    'guests':b['guests'],'children':b['children'],'name':b['name'],'phone':b['phone']}
        d = dict(data)
        d.update(cart={}, rental_added=bool(booking), consent=False, base_hours=d.get('hours',2))
        token = uid()[:12]
        async with self.s.write() as c:
            await c.execute('INSERT INTO checkout_draft VALUES($1,$2,$3::jsonb,now()) ON CONFLICT(telegram_id) DO UPDATE SET token=$2,data=$3::jsonb,updated_at=now()',actor,token,json.dumps(d))
        await self.s.set_session(actor,'',{})
        return token

    async def read(self,actor,token=None,c=None):
        r=await (c or self.s.pool).fetchrow('SELECT token,data FROM checkout_draft WHERE telegram_id=$1'+(' FOR UPDATE' if c else ''),actor)
        if not r or (token and r['token']!=token):raise RuleError('Эта корзина устарела. Откройте «Корзина» в главном меню.')
        return r['token'],json.loads(r['data'])

    async def save(self,c,actor,d):
        await c.execute('UPDATE checkout_draft SET data=$2::jsonb,updated_at=now() WHERE telegram_id=$1',actor,json.dumps(d))

    async def rent(self,d,c):
        start=datetime.fromisoformat(d['start'])
        if not d.get('booking'):return (await self.s.quote(start,d['hours'],d['guests'],d['children'],c))[0]
        b=await self.s.get(d['booking'],d['_actor'],c)
        old=int((b['end_at']-b['start_at']).total_seconds()/3600)
        if d['hours']<old:raise RuleError('Длительность заказа изменилась. Откройте товары из актуальной брони.')
        before=(await self.s.quote(start,old,b['guests'],b['children'],c))[0]
        after=(await self.s.quote(start,d['hours'],b['guests'],b['children'],c))[0]
        return b['rent']+after-before

    async def quote(self,actor,token=None):
        async with self.s.write() as c:
            token,d=await self.read(actor,token,c)
            if d.get('submitted'):return token,d
            d['_actor']=actor;rent=await self.rent(d,c)
            changed=d.get('quote') is not None and d['quote']!=rent
            d['quote']=rent
            for code,line in d['cart'].items():
                item=await c.fetchrow('SELECT * FROM service_catalog WHERE code=$1 AND active',code)
                if not item:raise RuleError('Выбранная услуга недоступна. Очистите черновик и выберите доступные товары.')
                changed=changed or line['price']!=item['price']
                line.update(name=item['name'],price=item['price'])
            if changed:d['consent']=False
            await self.save(c,actor,d)
            return token,d

    async def action(self,actor,token,action,value='',nonce=''):
        async with self.s.write() as c:
            _,d=await self.read(actor,token,c)
            if d.get('submitted'):raise RuleError('Заказ уже оформлен. Откройте «Мои бронирования».')
            if action=='product':
                item=await c.fetchrow('SELECT * FROM service_catalog WHERE code=$1 AND active',value)
                if not item:raise RuleError('Товар недоступен.')
                d['item']={'code':value,'qty':1,'nonce':uid()[:8],'price':item['price'],'name':item['name'],'added':False}
            elif action in ('plus','minus','add'):
                item=d.get('item',{})
                if item.get('nonce')!=nonce:raise RuleError('Карточка устарела. Выберите товар заново.')
                if item.get('added'):raise RuleError('Этот выбор уже добавлен. Для нового выбора откройте товар из каталога.')
                if action=='add':
                    live=await c.fetchrow('SELECT * FROM service_catalog WHERE code=$1 AND active',item['code'])
                    if not live or live['price']!=item['price']:raise RuleError('Цена или доступность изменились. Откройте товар заново.')
                    line=d['cart'].get(item['code'],{'quantity':0,'price':item['price'],'name':item['name']})
                    if line['price']!=item['price']:raise RuleError('Цена изменилась. Откройте корзину для нового расчёта.')
                    if line['quantity']+item['qty']>100:raise RuleError('Не более 100 единиц одного товара.')
                    line['quantity']+=item['qty'];d['cart'][item['code']]=line;item['added']=True
                else:
                    qty=item['qty']+(1 if action=='plus' else -1)
                    if not 1<=qty<=100:raise RuleError('Количество от 1 до 100.')
                    item['qty']=qty
                d['consent']=False
            elif action in ('hourplus','hourminus'):
                target=d['hours']+(1 if action=='hourplus' else -1)
                if not d['base_hours']<=target<=5:raise RuleError('Основной сеанс от 2 часов; общая длительность до 5 часов.')
                start=datetime.fromisoformat(d['start'])
                clock=(start-timedelta(hours=1)) if d.get('booking') else now()
                _,clean=window(start,target,clock,True)
                await self.s.available(c,start,clean,d.get('booking',''))
                d['hours']=target;d['consent']=False
            elif action=='rental':d['rental_added']=True
            elif action=='clear':
                d['cart']={};d['item']={};d['hours']=d['base_hours'];d['consent']=False
                if not d.get('booking'):d['rental_added']=False
            elif action=='consent':d['consent']=not d.get('consent',False)
            else:raise RuleError('Неизвестное действие.')
            await self.save(c,actor,d)
            return d

    async def contacts(self,actor,token,data):
        async with self.s.write() as c:
            _,d=await self.read(actor,token,c)
            if d.get('submitted'):raise RuleError('Заказ уже оформлен.')
            for key in ('name','phone'):d[key]=data[key]
            d['consent']=False
            await self.save(c,actor,d)

    async def checkout(self,actor,token,rules_hash):
        async with self.s.write() as c:
            _,d=await self.read(actor,token,c)
            if d.get('submitted'):return d['submitted']
            if not d['rental_added']:raise RuleError('Сначала добавьте сеанс бани.')
            if not d.get('consent'):raise RuleError('Ознакомьтесь с правилами и отметьте согласие.')
            if not d.get('name') or not d.get('phone'):raise RuleError('Укажите имя и подтвердите телефон.')
            d['_actor']=actor;rent=await self.rent(d,c)
            if rent!=d.get('quote'):raise RuleError('Тариф изменился. Откройте корзину и подтвердите новый расчёт.')
            for code,line in d['cart'].items():
                live=await c.fetchrow('SELECT price FROM service_catalog WHERE code=$1 AND active',code)
                if not live or live['price']!=line['price']:raise RuleError('Цена услуги изменилась. Откройте корзину и подтвердите новый расчёт.')
            if d.get('booking'):
                ident=d['booking'];b=await self.s.get(ident,actor,c)
                if b['guest_id']!=actor:raise RuleError('Это действие гостя заказа.')
                old=int((b['end_at']-b['start_at']).total_seconds()/3600)
                if d['hours']>old:await self.duration(c,actor,ident,d['hours'],False,rent)
                elif not d['cart']:raise RuleError('Выберите товары или дополнительное время.')
            else:
                d['guest_id']=actor
                ident=await self.s._hold(c,actor,d,rules_hash,f'checkout:{actor}:{token}',requested_long=d['hours']>2)
                if d['hours']>2:
                    await c.execute('UPDATE booking SET duration_approved=false WHERE id=$1',ident)
                    await self.duration(c,actor,ident,d['hours'],True,rent)
            if d['cart']:await self.s._service_basket(c,actor,ident,{k:v['quantity'] for k,v in d['cart'].items()},f'checkout:{actor}:{token}')
            # Pending duration approval is allowed to create a link request; actual URL delivery is guarded.
            await self.s._request_link(c,actor,ident)
            d['submitted']=ident;await self.save(c,actor,d)
            return ident

    async def duration(self,c,actor,ident,hours,initial,rent):
        b=await self.s.get(ident,actor,c)
        if b['status'] not in ('held','payment_review','review_overdue','confirmed','in_progress'):raise RuleError('Заказ закрыт.')
        existing=await c.fetchval("SELECT id FROM duration_request WHERE booking_id=$1 AND status='pending'",ident)
        if existing:raise RuleError('Предыдущее изменение длительности ещё на рассмотрении.')
        start=b['start_at'].astimezone(now().tzinfo);_,clean=window(start,hours,start-timedelta(hours=1),True)
        await self.s.available(c,start,clean,ident)
        rid=uid()
        await c.execute('INSERT INTO duration_request(id,booking_id,target_hours,initial,quoted_rent) VALUES($1,$2,$3,$4,$5)',rid,ident,hours,initial,rent)
        await self.s.notify_staff(c,f'Запрос аренды на {hours} ч. Проверка времени и уборки обязательна.\n/order {ident}',f'duration:{rid}')
        await self.s.audit(c,actor,'request_duration',rid,{'hours':hours,'initial':initial})

    async def decide_duration(self,actor,rid,approve):
        async with self.s.write() as c:
            await self.s.staff(actor,c)
            r=await c.fetchrow('SELECT * FROM duration_request WHERE id=$1',rid)
            if not r:raise RuleError('Запрос не найден.')
            if r['status']!='pending':return r['booking_id']
            b=await self.s.get(r['booking_id'],actor,c)
            if b['status'] not in ('held','payment_review','review_overdue','confirmed','in_progress'):raise RuleError('Заказ уже закрыт.')
            clock=await c.fetchval('SELECT clock_timestamp()')
            if b['status']=='held' and b['release_at']<=clock:raise RuleError('Резерв истёк. Откройте актуальную бронь.')
            if approve:
                start=b['start_at'].astimezone(now().tzinfo)
                end,clean=window(start,r['target_hours'],start-timedelta(hours=1),True)
                if end<=clock:raise RuleError('Сеанс уже завершился.')
                await self.s.available(c,start,clean,b['id'])
                if r['initial']:await c.execute('UPDATE booking SET duration_approved=true WHERE id=$1',b['id'])
                else:
                    d={'booking':b['id'],'start':start.isoformat(),'hours':r['target_hours'],'_actor':actor}
                    rent=await self.rent(d,c)
                    if rent!=r['quoted_rent']:raise RuleError('Цена продления изменилась. Согласуйте новый расчёт с гостем и создайте новый запрос.')
                    old=int((b['end_at']-b['start_at']).total_seconds()/3600)
                    _,fresh=await self.s.quote(start,r['target_hours'],b['guests'],b['children'],c)
                    lines=json.loads(b['price_lines'])
                    lines.extend(x for x in fresh if x['kind']=='rent' and datetime.fromisoformat(x['hour'])>=b['end_at'])
                    lines.append({'kind':'extension_guests','amount':max(0,b['guests']-b['children']-4)*25000*(r['target_hours']-old)})
                    await c.execute('UPDATE booking SET end_at=$2,cleanup_end=$3,rent=$4,price_lines=$5::jsonb,revision=revision+1 WHERE id=$1',b['id'],end,clean,rent,json.dumps(lines))
                    await self.s.reminders(c,await self.s.get(b['id'],actor,c))
                text=f'Длительность {r["target_hours"]} ч. подтверждена администратором.'
            else:
                if r['initial']:
                    paid=await c.fetchval('SELECT COALESCE(sum(amount),0) FROM receipt WHERE booking_id=$1',b['id'])
                    pending=await c.fetchval("SELECT EXISTS(SELECT 1 FROM payment_claim WHERE booking_id=$1 AND status='pending')",b['id'])
                    if paid or pending:raise RuleError('Есть оплата или заявление. Сначала проведите финансовую сверку; автоматический отказ заблокирован.')
                    await c.execute("UPDATE booking SET status='rejected',revision=revision+1 WHERE id=$1",b['id'])
                    await c.execute("UPDATE payment_link_request SET status='closed' WHERE booking_id=$1 AND status='pending'",b['id'])
                if r['initial']:await c.execute("UPDATE booking_service SET status='rejected' WHERE booking_id=$1 AND status='requested'",b['id'])
                text='Дополнительная длительность не подтверждена.'+(' Выберите новый сеанс.' if r['initial'] else ' Исходное время брони сохраняется.')
            await c.execute('UPDATE duration_request SET status=$2,actor=$3 WHERE id=$1',rid,'approved' if approve else 'rejected',actor)
            await self.s.queue(c,b['guest_id'],text+f'\n/order {b["id"]}',f'duration-decision:{rid}')
            await self.s.audit(c,actor,'duration_decision',rid,{'approve':approve})
            return b['id']
