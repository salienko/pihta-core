"""Manual payment links and receipts; money moves only after staff verification."""
import re
import secrets
from urllib.parse import urlsplit
from datetime import timedelta
from booking_domain import RuleError, rub, card

DEPOSIT = 200000


def total(row):
    return row['actual_total'] if row.get('actual_total') is not None else row['estimate']['total']


def payment_url(text):
    value=text.strip()
    parsed=urlsplit(value)
    if (len(value)>2000 or re.search(r'\s',value) or parsed.scheme!='https' or not parsed.hostname
            or parsed.username or parsed.password or '.' not in parsed.hostname):
        raise RuleError('Отправьте одну полную HTTPS-ссылку для оплаты, без другого текста.')
    return value


class PaymentStore:
    async def request_payment(self,actor,ident,kind):
        from booking_store import unpack
        if kind not in ('deposit','full','balance'):raise RuleError('Выберите способ оплаты.')
        async with self.tx() as db:
            clock=await self.clock(db);await self.expire(db,clock)
            row=unpack(await db.fetchrow('SELECT * FROM bookings WHERE id=$1 FOR UPDATE',ident))
            if not row or row['telegram_id']!=actor:raise RuleError('Бронь недоступна.')
            if row['status'] not in ('pending','confirmed') or row['ends_at']<=clock:raise RuleError('Бронь уже закрыта.')
            due=max(0,total(row)-row['paid'])
            if not due:raise RuleError('Вся стоимость уже внесена.')
            if row['paid'] and kind!='balance':raise RuleError('Предоплата уже учтена. Запросите ссылку на остаток.')
            if not row['paid'] and kind=='balance':raise RuleError('Выберите предоплату или полную оплату.')
            amount=min(DEPOSIT,due) if kind=='deposit' else due
            old=await db.fetchrow("SELECT * FROM payment_requests WHERE booking_id=$1 AND kind=$2 AND amount=$3 AND paid_before=$4 AND state IN ('requested','linked','reported') ORDER BY created_at DESC LIMIT 1",ident,kind,amount,row['paid'])
            if old:return dict(old),False
            request_id=secrets.token_hex(6)
            req=await db.fetchrow("INSERT INTO payment_requests(id,booking_id,kind,amount,paid_before) VALUES($1,$2,$3,$4,$5) RETURNING *",request_id,ident,kind,amount,row['paid'])
            if self.settings.paykeeper_enabled:
                payment_type=getattr(self.settings,'paykeeper_'+kind+'_type')
                # счёт выставляется на исправленный телефон, если гость его менял
                await db.execute('INSERT INTO paykeeper_invoices(request_id,booking_id,actor,amount,paid_before,client_id,phone,payment_type) VALUES($1,$2,$3,$4,$5,$6,$7,$8)',
                                 request_id,ident,actor,amount,row['paid'],row['data']['name'],
                                 row.get('contact_phone') or row['data']['phone'],payment_type)
                await self.audit(db,ident,actor,'paykeeper_invoice_requested',dict(request_id=request_id,kind=kind,amount=amount))
                return dict(req),True
            label={'deposit':'предоплата','full':'полная оплата','balance':'доплата остатка'}[kind]
            text=(f'💳 Запрос ссылки · {label}\nБронь № {ident}\n👤 {row["data"]["name"]}\n📱 {row.get("contact_phone") or row["data"]["phone"]}\n'
                  f'Всего: {rub(total(row))}\nВнесено: {rub(row["paid"])}\nОсталось: {rub(due)}\n'
                  f'Ссылка нужна на: {rub(amount)}\n\nОтветьте на это сообщение одной HTTPS-ссылкой для оплаты.')
            await self.enqueue(db,row,'payreq:'+request_id,text,self.settings.admin_ids)
            await self.audit(db,ident,actor,'payment_link_requested',dict(request_id=request_id,kind=kind,amount=amount))
            return dict(req),True

    async def payment_requests(self,actor,ident):
        await self.get(actor,ident)
        return [dict(r) for r in await self.pool.fetch("SELECT * FROM payment_requests WHERE booking_id=$1 AND state IN ('requested','linked','reported') ORDER BY created_at DESC",ident)]

    async def link_payment(self,actor,reply_message_id,url):
        from booking_store import unpack
        self.admin(actor);url=payment_url(url)
        async with self.tx() as db:
            clock=await self.clock(db);await self.expire(db,clock)
            req=await db.fetchrow('SELECT p.* FROM payment_requests p JOIN payment_messages m ON m.request_id=p.id WHERE m.chat_id=$1 AND m.message_id=$2 FOR UPDATE OF p',actor,reply_message_id)
            if not req:raise RuleError('Ответьте ссылкой именно на сообщение с запросом оплаты.')
            row=unpack(await db.fetchrow('SELECT * FROM bookings WHERE id=$1 FOR UPDATE',req['booking_id']))
            if row['status'] not in ('pending','confirmed') or req['paid_before']!=row['paid'] or req['state'] not in ('requested','linked'):
                raise RuleError('Запрос уже закрыт или сумма изменилась. Откройте актуальную бронь.')
            if req['url']==url:return dict(req),False
            revision=req['revision']+1
            await db.execute("UPDATE payment_requests SET url=$1,state='linked',revision=$2 WHERE id=$3",url,revision,req['id'])
            await self.enqueue(db,row,f'paylink:{req["id"]}:{revision}',f'🔗 Ссылка для оплаты брони № {row["id"]}\nСумма: {rub(req["amount"])}\n\nПосле оплаты нажмите «✅ Уже оплатил».',[row['telegram_id']])
            await self.audit(db,row['id'],actor,'payment_link_sent',dict(request_id=req['id'],amount=req['amount']))
            return dict(req),True

    async def report_payment(self,actor,request_id):
        from booking_store import unpack
        async with self.tx() as db:
            clock=await self.clock(db);await self.expire(db,clock)
            req=await db.fetchrow('SELECT * FROM payment_requests WHERE id=$1 FOR UPDATE',request_id)
            if not req:raise RuleError('Запрос не найден.')
            row=unpack(await db.fetchrow('SELECT * FROM bookings WHERE id=$1 FOR UPDATE',req['booking_id']))
            if row['telegram_id']!=actor:raise RuleError('Бронь недоступна.')
            if row['status'] not in ('pending','confirmed') or row['paid']!=req['paid_before']:raise RuleError('Проверьте актуальную оплату в карточке брони.')
            if req['state']=='reported':return row['id'],False
            if req['state']!='linked':raise RuleError('Сначала получите актуальную ссылку для оплаты.')
            await db.execute("UPDATE payment_requests SET state='reported',revision=revision+1 WHERE id=$1",request_id)
            await self.enqueue(db,row,f'payreported:{request_id}:{req["revision"]+1}',f'🔔 Гость сообщил об оплате\nБронь № {row["id"]}\n👤 {row["data"]["name"]}\nОжидаемый платёж: {rub(req["amount"])}\nРанее внесено: {rub(row["paid"])}\n\nПроверьте фактическое поступление денег и подтвердите оплату.',self.settings.admin_ids)
            await self.audit(db,row['id'],actor,'payment_reported',dict(request_id=request_id))
            return row['id'],True

    async def decide_payment(self,actor,request_id,received):
        from booking_store import unpack
        self.admin(actor)
        async with self.tx() as db:
            clock=await self.clock(db);await self.expire(db,clock)
            req=await db.fetchrow('SELECT * FROM payment_requests WHERE id=$1 FOR UPDATE',request_id)
            if not req:raise RuleError('Запрос не найден.')
            row=unpack(await db.fetchrow('SELECT * FROM bookings WHERE id=$1 FOR UPDATE',req['booking_id']))
            if req['state']=='paid':return row['id'],False
            if row['status'] not in ('pending','confirmed') or req['paid_before']!=row['paid'] or req['state'] not in ('linked','reported'):
                raise RuleError('Запрос устарел или бронь закрыта. Проверьте расчёт в веб-панели.')
            if not received:
                if req['state']=='linked':return row['id'],False
                await db.execute("UPDATE payment_requests SET state='linked',revision=revision+1 WHERE id=$1",request_id)
                await self.enqueue(db,row,f'paymissing:{request_id}:{req["revision"]+1}','Администратор пока не видит поступление оплаты по брони № '+row['id']+'. Проверьте платёж. Ссылка остаётся доступна; после проверки можно снова нажать «Уже оплатил».',[row['telegram_id']])
                await self.audit(db,row['id'],actor,'payment_not_received',dict(request_id=request_id))
                return row['id'],True
            amount=req['amount'];paid=row['paid']+amount
            if paid>total(row):raise RuleError('Сумма изменилась. Проверьте расчёт в веб-панели.')
            status='confirmed' if paid>=min(DEPOSIT,total(row)) else row['status']
            await db.execute('UPDATE bookings SET paid=$1,status=$2,revision=revision+1 WHERE id=$3',paid,status,row['id'])
            await db.execute('INSERT INTO operations VALUES($1,$2,$3)', 'payreq:'+request_id,actor,row['id'])
            await db.execute("UPDATE payment_requests SET state=CASE WHEN id=$1 THEN 'paid' ELSE 'superseded' END,revision=revision+1 WHERE booking_id=$2 AND state IN ('requested','linked','reported')",request_id,row['id'])
            await db.execute("UPDATE outbox SET disabled=true WHERE booking_id=$1 AND delivered_at IS NULL AND (event LIKE 'payreq:%' OR event LIKE 'paylink:%' OR event LIKE 'payreported:%' OR event LIKE 'reminder:pending:%')",row['id'])
            await self.audit(db,row['id'],actor,'payment',dict(amount=amount,request_id=request_id,kind=req['kind'],note='Поступление проверено администратором'))
            updated=unpack(await db.fetchrow('SELECT * FROM bookings WHERE id=$1',row['id']))
            await self.enqueue(db,updated,'payconfirmed:'+request_id,card(updated),[row['telegram_id']])
            return row['id'],True

    async def payment_delivery(self,event):
        if not event.startswith(('payreq:','paylink:','payreported:')):return None
        parts=event.split(':');req=await self.pool.fetchrow('SELECT * FROM payment_requests WHERE id=$1',parts[1])
        if not req:return None
        row=await self.pool.fetchrow('SELECT status,paid FROM bookings WHERE id=$1',req['booking_id'])
        if not row or row['status'] not in ('pending','confirmed') or row['paid']!=req['paid_before'] or req['state'] not in ('requested','linked','reported'):return None
        if event.startswith('payreported:') and (req['state']!='reported' or int(parts[2])!=req['revision']):return None
        if event.startswith('paylink:') and (not req['url'] or int(parts[2])>req['revision']):return None
        return dict(req)

    async def payment_message_delivered(self,request_id,chat_id,message_id):
        await self.pool.execute('INSERT INTO payment_messages VALUES($1,$2,$3) ON CONFLICT DO NOTHING',chat_id,message_id,request_id)

    async def request_cancellation(self,actor,ident):
        from booking_store import unpack
        async with self.tx() as db:
            clock=await self.clock(db);await self.expire(db,clock)
            row=unpack(await db.fetchrow('SELECT * FROM bookings WHERE id=$1 FOR UPDATE',ident))
            if not row or row['telegram_id']!=actor:raise RuleError('Бронь недоступна.')
            if row['status'] not in ('pending','confirmed'):raise RuleError('Бронь уже закрыта.')
            key='cancelreq:'+ident
            if await db.fetchval('SELECT key FROM operations WHERE key=$1',key):return False
            await db.execute('INSERT INTO operations VALUES($1,$2,$3)',key,actor,ident)
            await self.enqueue(db,row,'cancel_requested','❗ Гость просит отменить бронирование\n\n'+card(row)+'\n\nОтмените вручную в веб-панели. Проверьте внесённые деньги и необходимость возврата.',self.settings.admin_ids)
            await self.audit(db,ident,actor,'cancellation_requested')
            return True
