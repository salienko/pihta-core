"""Durable PayKeeper invoices, payment ledger, receipt discovery and notifications."""
import re
import secrets
from booking_domain import RuleError, rub, normalize_phone,payment_confirmation
from booking_payments import DEPOSIT, total
from paykeeper import PayKeeper, InvoiceNotSent

ACCEPTED = 'Пришлём чек отдельным сообщением, как только он будет готов.'
ACCEPTED_READY = 'Ваш чек доступен по кнопке ниже.'
RECEIPT_READY = 'Ваш чек сформирован и доступен по кнопке ниже.'
# Сколько раз автоматически спрашиваем ОФД, прежде чем признать чек несформированным.
# Интервал растёт: 2, 4, 6, 8, 10, 12 минут — примерно 40 минут на платёж.
RECEIPT_ATTEMPTS = 6
RECEIPT_DELAY = 'make_interval(mins=>LEAST(60,2*receipt_attempts+2))'


async def next_order_id(db):
    """Человекочитаемый номер заказа для PayKeeper: P-2026-000042.

    Внутренний request_id остаётся hex-ключом, но гость и кассир видят
    в счёте понятный номер, а не строку из случайных символов.
    """
    return await db.fetchval(
        "SELECT 'P-'||to_char(now(),'YYYY')||'-'||lpad(nextval('paykeeper_order_seq')::text,6,'0')")


class PayKeeperStore:
    def provider(self):
        return PayKeeper(self.settings)

    async def pk_context(self, db, invoice):
        from booking_store import unpack
        if invoice['booking_id']:
            return unpack(await db.fetchrow('SELECT * FROM bookings WHERE id=$1 FOR UPDATE', invoice['booking_id']))
        return dict(id=None, telegram_id=None)

    async def pk_problem(self, db, invoice, text):
        row=await self.pk_context(db,invoice)
        await self.enqueue(db,row,'pkerror:'+invoice['request_id'],text,self.settings.admin_ids)

    async def invoice_once(self):
        if not self.settings.paykeeper_enabled:return False
        async with self.tx() as db:
            stale=await db.fetch("UPDATE paykeeper_invoices SET state='uncertain',last_error='interrupted_creation' WHERE state='creating' AND started_at<now()-interval '2 minutes' RETURNING *")
            for i in stale:
                await self.pk_problem(db,i,'⚠️ Проверьте результат создания счёта. Запрос '+i['request_id']+'. Автоматический повтор отключён, чтобы не создавать дубликат.')
            item=await db.fetchrow("SELECT * FROM paykeeper_invoices WHERE state='queued' AND next_check_at<=now() ORDER BY created_at LIMIT 1 FOR UPDATE")
            if not item:return False
            item=dict(item);row=await self.pk_context(db,item)
            if item['booking_id'] and (row['status'] not in ('pending','confirmed') or row['ends_at']<=await self.clock(db) or row['paid']!=item['paid_before']):
                await db.execute("UPDATE paykeeper_invoices SET state='expired' WHERE request_id=$1",item['request_id'])
                return True
            await db.execute("UPDATE paykeeper_invoices SET state='creating',started_at=now() WHERE request_id=$1",item['request_id'])
        try:
            invoice_id,url=await self.provider().invoice(item)
        except InvoiceNotSent:
            async with self.tx() as db:
                await db.execute("UPDATE paykeeper_invoices SET state='queued',next_check_at=now()+interval '2 minutes',last_error='token_request_failed' WHERE request_id=$1 AND state='creating'",item['request_id'])
                await self.pk_problem(db,item,'⚠️ Сейчас мы не можем получить ссылку на оплату. Запрос '+item['request_id']+'. Создание счёта ещё не отправлялось, повторим позже.')
            return True
        except Exception:
            async with self.tx() as db:
                await db.execute("UPDATE paykeeper_invoices SET state='uncertain',last_error='creation_outcome_unknown' WHERE request_id=$1 AND state='creating'",item['request_id'])
                await self.pk_problem(db,item,'⚠️ Ссылка на оплату не получена. Запрос '+item['request_id']+'. Сначала сверьте результат в платёжном сервисе, не создавайте повторный счёт без проверки.')
                row=await self.pk_context(db,item)
                if row['id']:
                    await self.enqueue(db,row,'pkunavailable:'+item['request_id'],'Ссылка на оплату пока недоступна. Администратор уведомлён. Бронь сохранена.',[row['telegram_id']])
            return True
        await self.attach_invoice(item,invoice_id,url)
        return True

    async def attach_invoice(self,item,invoice_id,url):
        async with self.tx() as db:
            current=await db.fetchrow('SELECT invoice_id FROM paykeeper_invoices WHERE request_id=$1 FOR UPDATE',item['request_id'])
            if not current or (current['invoice_id'] and current['invoice_id']!=invoice_id):raise RuleError('К запросу уже привязан другой счёт. Нужна сверка.')
            await db.execute("UPDATE paykeeper_invoices SET invoice_id=$2,url=$3,state=CASE WHEN state='received' THEN state ELSE 'ready' END,last_error='' WHERE request_id=$1",item['request_id'],invoice_id,url)
            row=await self.pk_context(db,item)
            if not row['id']:return
            req=await db.fetchrow('SELECT * FROM payment_requests WHERE id=$1',item['request_id'])
            if not req or req['state']!='requested' or row['status'] not in ('pending','confirmed') or row['ends_at']<=await self.clock(db) or row['paid']!=item['paid_before']:return
            await db.execute("UPDATE payment_requests SET state='linked',url=$2,revision=revision+1 WHERE id=$1",req['id'],url)
            await self.enqueue(db,row,'paylink:'+req['id']+':'+str(req['revision']+1),
                               '💳 Оплата брони № '+row['id']+'\nСумма: '+rub(item['amount'])+'\nПосле оплаты подтверждение поступит автоматически.',[row['telegram_id']])

    async def accept_paykeeper(self,values,amount):
        from booking_store import unpack
        async with self.tx() as db:
            clock=await self.clock(db);await self.expire(db,clock)
            old=await db.fetchrow('SELECT * FROM paykeeper_payments WHERE payment_id=$1',values['id'])
            if old:
                if (old['request_id'],old['amount'])!=((values.get('request_id') or values['orderid']),amount):raise ValueError('conflicting_payment')
                return False
            old=await db.fetchrow('SELECT * FROM paykeeper_unmatched WHERE payment_id=$1',values['id'])
            if old:
                if (old['order_id'],old['amount'])!=(values['orderid'],amount):raise ValueError('conflicting_unmatched_payment')
                return False
            i=await db.fetchrow('SELECT * FROM paykeeper_invoices WHERE order_id=$1 OR request_id=$1 FOR UPDATE',values['orderid'])
            if not i:
                await db.execute('INSERT INTO paykeeper_unmatched(payment_id,order_id,client_id,amount) VALUES($1,$2,$3,$4)',values['id'],values['orderid'],values.get('clientid') or '',amount)
                await self.enqueue(db,dict(id=None),'pkunmatched:'+values['id'],'⚠️ Платёж № '+values['id']+' на '+rub(amount)+' не связан со счётом бота. Проверьте поступление в платёжном сервисе. Брони не изменены.',self.settings.admin_ids)
                return False
            # clientid больше не выставляется в счёте, поэтому платёж сверяем по номеру заказа и сумме.
            if i['amount']!=amount:raise ValueError('invoice_mismatch')
            row=await self.pk_context(db,i)
            prior=await db.fetchval('SELECT EXISTS(SELECT 1 FROM paykeeper_payments WHERE request_id=$1)',i['request_id'])
            req=await db.fetchrow('SELECT * FROM payment_requests WHERE id=$1 FOR UPDATE',i['request_id'])
            apply=bool(row['id'] and not prior and row['status'] in ('pending','confirmed') and row['ends_at']>clock
                       and row['paid']==i['paid_before'] and row['paid']+amount<=total(row)
                       and (not req or req['state'] in ('requested','linked','reported')))
            disposition='applied' if apply else 'standalone' if not row['id'] and not prior else 'review'
            await db.execute('INSERT INTO paykeeper_payments(payment_id,request_id,client_id,amount,disposition) VALUES($1,$2,$3,$4,$5)',values['id'],i['request_id'],values.get('clientid') or '',amount,disposition)
            await db.execute("UPDATE paykeeper_invoices SET state='received' WHERE request_id=$1",i['request_id'])
            if apply:
                paid=row['paid']+amount;status='confirmed' if paid>=min(DEPOSIT,total(row)) else row['status']
                await db.execute('UPDATE bookings SET paid=$2,status=$3,revision=revision+1 WHERE id=$1',row['id'],paid,status)
                await db.execute("UPDATE payment_requests SET state=CASE WHEN id=$2 THEN 'paid' ELSE 'superseded' END,revision=revision+1 WHERE booking_id=$1 AND state IN ('requested','linked','reported')",row['id'],i['request_id'])
                await db.execute("UPDATE outbox SET disabled=true WHERE booking_id=$1 AND delivered_at IS NULL AND (event LIKE 'payreq:%' OR event LIKE 'paylink:%' OR event LIKE 'payreported:%' OR event LIKE 'reminder:pending:%')",row['id'])
                updated=unpack(await db.fetchrow('SELECT * FROM bookings WHERE id=$1',row['id']))
                await self.enqueue(db,row,'pkpaid:'+values['id'],payment_confirmation(updated)+' '+ACCEPTED,[row['telegram_id']])
            elif row['id']:
                await self.enqueue(db,row,'pkreviewguest:'+values['id'],'Ваша оплата принята. Спасибо. Администратор проверит её привязку к брони. Повторно оплачивать не нужно.',[row['telegram_id']])
            where='Бронь № '+row['id']+'. ' if row['id'] else ''
            text=('💰 Поступила оплата на сумму '+rub(amount)+', телефон '+i['phone']+'. '
                  if disposition!='review' else
                  '⚠️ Поступила оплата на сумму '+rub(amount)+', телефон '+i['phone']+', нужна сверка. ')
            text+=where+'Платёж № '+values['id']+', запрос '+i['request_id']+'.'
            await self.enqueue(db,row,'pkadmin:'+values['id'],text,self.settings.admin_ids)
            await self.audit(db,row['id'],0,'paykeeper_payment',dict(payment_id=values['id'],request_id=i['request_id'],amount=amount,disposition=disposition))
            return True

    async def poll_invoice(self,i,notify=True):
        result=await self.provider().invoice_status(i)
        status=result.get('status')
        if status=='paid':
            ident=str(result.get('paymentid',''))
            if not re.fullmatch(r'[0-9]{1,40}',ident):raise ValueError('missing_payment_id')
            await self.accept_paykeeper(dict(id=ident,orderid=i['order_id'] or i['request_id'],
                                             request_id=i['request_id'],clientid=i['client_id']),i['amount'])
        elif status=='expired':
            async with self.tx() as db:
                await db.execute("UPDATE paykeeper_invoices SET state='expired' WHERE request_id=$1 AND state='ready'",i['request_id'])
                await db.execute("UPDATE payment_requests SET state='superseded',revision=revision+1 WHERE id=$1 AND state IN ('requested','linked','reported')",i['request_id'])
                row=await self.pk_context(db,i)
                if notify and row['id'] and row['status'] in ('pending','confirmed'):
                    await self.enqueue(db,row,'pkinvoiceexpired:'+i['request_id'],'Срок счёта истёк. Бронь сохранена. При необходимости получите новую ссылку в карточке брони.')
        elif status not in ('created','sent'):raise ValueError('unknown_invoice_status')
        return status

    async def poll_once(self):
        if not self.settings.paykeeper_enabled:return False
        async with self.tx() as db:
            i=await db.fetchrow("UPDATE paykeeper_invoices SET next_check_at=now()+interval '20 minutes' WHERE request_id=(SELECT request_id FROM paykeeper_invoices WHERE state='ready' AND next_check_at<=now() ORDER BY next_check_at LIMIT 1 FOR UPDATE) RETURNING *")
        if not i:return False
        try:
            await self.poll_invoice(dict(i))
            await self.pool.execute("UPDATE paykeeper_invoices SET last_error='' WHERE request_id=$1",i['request_id'])
        except Exception:
            async with self.tx() as db:
                await db.execute("UPDATE paykeeper_invoices SET last_error='status_check_failed' WHERE request_id=$1",i['request_id'])
                await self.pk_problem(db,i,'⚠️ Не удалось проверить счёт. Запрос '+i['request_id']+'. Повторим проверку; бронь не отменяется.')
        return True

    async def check_paykeeper(self,actor,request_id):
        async with self.tx() as db:
            i=await db.fetchrow('SELECT * FROM paykeeper_invoices WHERE request_id=$1 FOR UPDATE',request_id)
            if not i:raise RuleError('Счёт не найден.')
            row=await self.pk_context(db,i)
            if row.get('telegram_id') is None or not await self.owns(db,actor,row['telegram_id']):raise RuleError('Счёт недоступен.')
            if i['state']=='received':return 'paid'
            if i['state']=='expired':return 'expired'
            if i['state']!='ready':raise RuleError('Ссылка ещё готовится. Администратор уведомлён при ошибке.')
            last=await db.fetchval('SELECT result FROM operations WHERE key=$1','pkcheck:'+request_id)
            now=await self.clock(db)
            if last and float(last)>now.timestamp()-15:raise RuleError('Проверка уже выполняется. Повторите через 15 секунд.')
            await db.execute('INSERT INTO operations VALUES($1,$2,$3) ON CONFLICT(key) DO UPDATE SET result=$3','pkcheck:'+request_id,actor,str(now.timestamp()))
            await self.enqueue(db,row,'pkguestcheck:'+request_id,'Гость запросил проверку оплаты '+rub(i['amount'])+' по брони № '+row['id']+'. Проверяем счёт в платёжном сервисе; нажатие кнопки не подтверждает поступление денег.',self.settings.admin_ids)
        try:return await self.poll_invoice(dict(i))
        except Exception:raise RuleError('Сейчас мы не можем проверить оплату. Повторите проверку позже; бронь сохранена.') from None

    async def receipt_query(self,payment):
        """Сеть: ищем чек в ОФД по номеру платежа. Ошибка провайдера не раскрывается наружу."""
        payment=dict(payment)
        issued_at=await self.pool.fetchval('SELECT created_at FROM paykeeper_invoices WHERE request_id=$1',payment.get('request_id') or '')
        if issued_at:payment['created_at']=min(payment['created_at'],issued_at)
        return await self.provider().receipts(payment)

    async def receipt_close(self,db,row,payment,state,error):
        """Терминальное состояние чека: автоматические опросы ОФД по платежу прекращаются."""
        await db.execute('UPDATE paykeeper_payments SET receipt_state=$2,receipt_error=$3,receipt_checked_at=now() WHERE payment_id=$1',payment['payment_id'],state,error)
        text=('⚠️ Чек платежа № '+payment['payment_id']+' не сформирован в кассе. Деньги учтены. Проверьте кассу; перепроверить можно в разделе «Счета и чеки».'
              if state=='missing' else
              '⚠️ Не удалось проверить чек платежа № '+payment['payment_id']+' в ОФД. Деньги учтены. Перепроверьте вручную в разделе «Счета и чеки».')
        await self.enqueue(db,row,'pkreceiptmissing:'+payment['payment_id'],text,self.settings.admin_ids)

    async def check_receipt(self,payment):
        """Один запрос в ОФД по платежу и запись результата. Возвращает новое состояние чека."""
        try:receipts=await self.receipt_query(payment)
        except Exception:receipts=None
        async with self.tx() as db:
            i=await db.fetchrow('SELECT * FROM paykeeper_invoices WHERE request_id=$1',payment['request_id'])
            row=await self.pk_context(db,i)
            if not receipts:
                if payment['receipt_attempts']>=RECEIPT_ATTEMPTS:
                    state='failed' if receipts is None else 'missing'
                    await self.receipt_close(db,row,payment,state,'receipt_check_failed' if receipts is None else '')
                    return state
                await db.execute('UPDATE paykeeper_payments SET receipt_error=$2,receipt_checked_at=now() WHERE payment_id=$1',
                                 payment['payment_id'],'receipt_check_failed' if receipts is None else '')
                return 'waiting'
            for receipt in receipts:
                previous=await db.fetchval('SELECT payment_id FROM paykeeper_fiscal_receipts WHERE id=$1',receipt['id'])
                if previous and previous!=payment['payment_id']:raise ValueError('receipt_payment_conflict')
                inserted=await db.fetchval('INSERT INTO paykeeper_fiscal_receipts(id,payment_id,url) VALUES($1,$2,$3) ON CONFLICT DO NOTHING RETURNING id',receipt['id'],payment['payment_id'],receipt['url'])
                if not inserted:continue
                merged=False
                if row['id'] and payment['disposition']=='applied':
                    merged=bool(await db.fetchval("UPDATE outbox SET text=$3 WHERE booking_id=$1 AND recipient=$2 AND event=$4 AND delivered_at IS NULL AND locked_until IS NULL AND attempts=0 RETURNING id",row['id'],row['telegram_id'],payment_confirmation(row)+' '+ACCEPTED_READY,'pkpaid:'+payment['payment_id']))
                if row['id'] and not merged:
                    await self.enqueue(db,row,'pkreceipt:'+receipt['id'],RECEIPT_READY,[row['telegram_id']])
                await self.enqueue(db,row,'pkreceiptadmin:'+receipt['id'],'Чек платежа № '+payment['payment_id']+' сформирован.',self.settings.admin_ids)
            await db.execute("UPDATE paykeeper_payments SET receipt_done=true,receipt_state='formed',receipt_error='',receipt_checked_at=now() WHERE payment_id=$1",payment['payment_id'])
        return 'formed'

    async def receipt_once(self):
        if not self.settings.paykeeper_enabled:return False
        async with self.tx() as db:
            p=await db.fetchrow("UPDATE paykeeper_payments SET receipt_attempts=receipt_attempts+1,receipt_checked_at=now(),"
                                "next_receipt_at=now()+"+RECEIPT_DELAY+" "
                                "WHERE payment_id=(SELECT payment_id FROM paykeeper_payments WHERE NOT receipt_done AND receipt_state='waiting' "
                                "AND next_receipt_at<=now() ORDER BY next_receipt_at LIMIT 1 FOR UPDATE) RETURNING *")
        if not p:return False
        await self.check_receipt(dict(p))
        return True

    async def recheck_receipt(self,actor,payment_id):
        """Ручная перепроверка чека из админки: сбрасывает лимит и сразу спрашивает ОФД."""
        self.admin(actor)
        if not re.fullmatch(r'[0-9]{1,40}',payment_id):raise RuleError('Проверьте номер платежа.')
        async with self.tx() as db:
            p=await db.fetchrow("UPDATE paykeeper_payments SET receipt_state='waiting',receipt_attempts=1,receipt_error='',"
                                "receipt_checked_at=now(),next_receipt_at=now()+interval '2 minutes' "
                                "WHERE payment_id=$1 AND NOT receipt_done RETURNING *",payment_id)
            if not p:raise RuleError('Чек уже получен или платёж не найден.')
            await self.audit(db,None,actor,'receipt_recheck',dict(payment_id=payment_id))
        return await self.check_receipt(dict(p))

    async def check_unmatched_receipt(self,payment):
        """Чек для неопознанного платежа: ищем по номеру платежа, брони у него нет."""
        try:receipts=await self.receipt_query(dict(payment,request_id=payment['order_id']))
        except Exception:receipts=None
        async with self.tx() as db:
            if receipts:
                await db.execute("UPDATE paykeeper_unmatched SET receipt_state='formed',receipt_url=$2,receipt_checked_at=now() WHERE payment_id=$1",payment['payment_id'],receipts[0]['url'])
                return 'formed'
            if payment['receipt_attempts']<RECEIPT_ATTEMPTS:return 'waiting'
            state='failed' if receipts is None else 'missing'
            await db.execute('UPDATE paykeeper_unmatched SET receipt_state=$2,receipt_checked_at=now() WHERE payment_id=$1',payment['payment_id'],state)
            return state

    async def unmatched_receipt_once(self):
        if not self.settings.paykeeper_enabled:return False
        async with self.tx() as db:
            u=await db.fetchrow("UPDATE paykeeper_unmatched SET receipt_attempts=receipt_attempts+1,receipt_checked_at=now(),"
                                "next_receipt_at=now()+"+RECEIPT_DELAY+" "
                                "WHERE payment_id=(SELECT payment_id FROM paykeeper_unmatched WHERE receipt_state='waiting' "
                                "AND next_receipt_at<=now() ORDER BY next_receipt_at LIMIT 1 FOR UPDATE) RETURNING *")
        if not u:return False
        await self.check_unmatched_receipt(dict(u))
        return True

    async def recheck_unmatched_receipt(self,actor,payment_id):
        """Ручная перепроверка чека для неопознанного платежа."""
        self.admin(actor)
        if not re.fullmatch(r'[0-9]{1,40}',payment_id):raise RuleError('Проверьте номер платежа.')
        async with self.tx() as db:
            u=await db.fetchrow("UPDATE paykeeper_unmatched SET receipt_state='waiting',receipt_attempts=1,"
                                "receipt_checked_at=now(),next_receipt_at=now()+interval '2 minutes' "
                                "WHERE payment_id=$1 AND receipt_state<>'formed' RETURNING *",payment_id)
            if not u:raise RuleError('Чек уже найден или платёж не найден.')
            await self.audit(db,None,actor,'receipt_recheck',dict(payment_id=payment_id,unmatched=True))
        return await self.check_unmatched_receipt(dict(u))

    async def payment_history(self,actor,booking_id,staff=False):
        await self.get(actor,booking_id,staff=staff)
        return [dict(r) for r in await self.pool.fetch('SELECT p.*,r.id AS receipt_id,r.url AS receipt_url FROM paykeeper_payments p JOIN paykeeper_invoices i USING(request_id) LEFT JOIN paykeeper_fiscal_receipts r USING(payment_id) WHERE i.booking_id=$1 ORDER BY p.created_at DESC',booking_id)]

    async def receipt_delivery_url(self,event):
        if event.startswith(('pkfinal:','pkfinalstaff:')):
            return await self.pool.fetchval("SELECT url FROM paykeeper_settlements WHERE booking_id=$1 AND state='success'",event.split(':')[1])
        if event.startswith(('pkreceipt:','pkreceiptadmin:')):
            return await self.pool.fetchval('SELECT url FROM paykeeper_fiscal_receipts WHERE id=$1',event.split(':')[1])
        if event.startswith('pkpaid:'):
            return await self.pool.fetchval('SELECT url FROM paykeeper_fiscal_receipts WHERE payment_id=$1 ORDER BY id LIMIT 1',event.split(':')[1])

    async def create_staff_invoice(self,actor,key,phone,amount,payment_type,email='',booking_id=None):
        self.admin(actor)
        if not self.settings.paykeeper_enabled:raise RuleError('Приём оплаты пока не включён.')
        if not re.fullmatch(r'[a-f0-9]{32}',key) or type(amount) is not int or not 0<amount<=100000000:raise RuleError('Проверьте сумму и обновите форму.')
        if payment_type not in ('full','prepay','part_prepay','advance'):raise RuleError('Выберите согласованный признак расчёта.')
        phone=normalize_phone(phone);email=email.strip()
        if email and (len(email)>254 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email)):raise RuleError('Проверьте e-mail.')
        request_id='staff'+key
        async with self.tx() as db:
            old=await db.fetchrow('SELECT * FROM paykeeper_invoices WHERE request_id=$1',request_id)
            if old:
                if (old['actor'],old['booking_id'],old['amount'],old['phone'],old['email'],old['payment_type'])!=(actor,booking_id,amount,phone,email,payment_type):raise RuleError('Эта форма уже использована с другими данными. Обновите страницу.')
                return dict(old)
            paid_before=0;client=''
            if booking_id:
                from booking_store import unpack
                row=unpack(await db.fetchrow('SELECT * FROM bookings WHERE id=$1',booking_id))
                if not row or row['status'] not in ('pending','confirmed') or row['ends_at']<=await self.clock(db):raise RuleError('Бронь закрыта или недоступна.')
                if phone!=row['data']['phone']:raise RuleError('Телефон должен совпадать с указанным в брони.')
                if amount>max(0,total(row)-row['paid']):raise RuleError('Сумма больше остатка по брони.')
                paid_before=row['paid'];client=row['data']['name']
            order_id=await next_order_id(db)
            i=await db.fetchrow('INSERT INTO paykeeper_invoices(request_id,booking_id,actor,amount,paid_before,client_id,phone,email,payment_type,order_id) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING *',request_id,booking_id,actor,amount,paid_before,client,phone,email,payment_type,order_id)
            await self.audit(db,booking_id,actor,'staff_invoice_requested',dict(request_id=request_id,amount=amount))
            return dict(i)

    async def resolve_invoice(self,actor,request_id,invoice_id):
        self.admin(actor)
        if not re.fullmatch(r'[0-9]{1,40}',invoice_id):raise RuleError('Проверьте номер счёта.')
        i=await self.pool.fetchrow("SELECT * FROM paykeeper_invoices WHERE request_id=$1 AND state='uncertain'",request_id)
        if not i:raise RuleError('Счёт не требует сверки.')
        item=dict(i);item['invoice_id']=invoice_id
        try:await self.provider().invoice_status(item)
        except Exception:raise RuleError('Счёт не прошёл проверку суммы, клиента и номера заказа.') from None
        await self.attach_invoice(item,invoice_id,self.settings.paykeeper_url+'/bill/'+invoice_id+'/')
