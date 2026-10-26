"""Durable guest reminders and payment-checked release of unpaid reservations."""
from datetime import timedelta
from booking_domain import TZ

LINK_REMINDER='reminder:pending:obtain_link'
PAY_REMINDER='reminder:pending:payment_deadline'


class ReserveStore:
    async def payment_message_sent(self,event,booking_id,recipient):
        if not event.startswith('paylink:') and event!=PAY_REMINDER:return
        async with self.tx() as db:
            row=await db.fetchrow('SELECT * FROM bookings WHERE id=$1',booking_id)
            if not row or recipient!=row['telegram_id'] or row['paid'] or row['status']!='pending':return
            now=await self.clock(db)
            if event.startswith('paylink:'):
                if not await db.fetchval('SELECT 1 FROM paykeeper_invoices WHERE request_id=$1 AND booking_id=$2',event.split(':')[1],booking_id):return
                await db.execute('INSERT INTO booking_payment_windows(booking_id,link_received_at) VALUES($1,$2) ON CONFLICT DO NOTHING',booking_id,now)
            elif event==PAY_REMINDER:
                await db.execute('UPDATE booking_payment_windows SET warned_at=COALESCE(warned_at,$2) WHERE booking_id=$1',booking_id,now)

    async def reserve_reminders(self,db,row,clock):
        if row['paid'] or row['status']!='pending' or row['telegram_id'] is None:return
        window=await db.fetchrow('SELECT * FROM booking_payment_windows WHERE booking_id=$1',row['id'])
        when=row['starts_at'].astimezone(TZ).strftime('%d.%m.%Y в %H:%M')
        if not window:
            # Legacy delivered links do not acquire a retrospective cancellation timer.
            delivered=await db.fetchval("SELECT 1 FROM outbox WHERE booking_id=$1 AND recipient=$2 AND event LIKE 'paylink:%' AND delivered_at IS NOT NULL LIMIT 1",row['id'],row['telegram_id'])
            if not delivered and clock-row['created_at']>=timedelta(minutes=15):
                await self.enqueue(db,row,LINK_REMINDER,'Вы забронировали баню «Пихта» на '+when+'. Для подтверждения бронирования внесите предоплату 2 000 ₽. Получить ссылку на оплату можно в меню «Мои бронирования».',[row['telegram_id']])
        elif clock-window['link_received_at']>=timedelta(minutes=10) and window['warned_at'] is None:
            await self.enqueue(db,row,PAY_REMINDER,'Мы пока не видим вашей оплаты по брони на '+when+'. Пожалуйста, внесите оплату в течение 10 минут по ссылке в меню «Мои бронирования». Если оплата не поступит, бронь отменится автоматически. Если уже оплатили, нажмите «Проверить оплату».',[row['telegram_id']])

    async def reserve_delivery_allowed(self,booking_id,event,recipient):
        if event not in (LINK_REMINDER,PAY_REMINDER):return True
        async with self.tx() as db:
            row=await db.fetchrow('SELECT * FROM bookings WHERE id=$1',booking_id)
            if not row or row['paid'] or row['status']!='pending' or row['telegram_id']!=recipient:return False
            if await db.fetchval('SELECT 1 FROM paykeeper_payments p JOIN paykeeper_invoices i USING(request_id) WHERE i.booking_id=$1 LIMIT 1',booking_id):return False
            window=await db.fetchrow('SELECT * FROM booking_payment_windows WHERE booking_id=$1',booking_id)
            return not window if event==LINK_REMINDER else bool(window and window['warned_at'] is None)

    async def reserve_once(self):
        if not self.settings.paykeeper_enabled:return False
        from booking_store import unpack
        async with self.tx() as db:
            clock=await self.clock(db)
            candidate=await db.fetchrow("SELECT w.* FROM booking_payment_windows w JOIN bookings b ON b.id=w.booking_id WHERE b.status='pending' AND b.paid=0 AND b.ends_at>$1 AND w.warned_at IS NOT NULL AND w.warned_at<=$1-interval '10 minutes' AND w.next_check_at<=$1 ORDER BY w.warned_at LIMIT 1 FOR UPDATE OF w",clock)
            if not candidate:return False
            ident=candidate['booking_id']
            await db.execute("UPDATE booking_payment_windows SET next_check_at=$2::timestamptz+interval '2 minutes' WHERE booking_id=$1",ident,clock)
            invoices=await db.fetch('SELECT * FROM paykeeper_invoices WHERE booking_id=$1',ident)
            if not invoices or any(i['state'] in ('queued','creating','uncertain') for i in invoices):return True
            if await db.fetchval('SELECT 1 FROM paykeeper_payments p JOIN paykeeper_invoices i USING(request_id) WHERE i.booking_id=$1 LIMIT 1',ident):return True
        checked=set()
        try:
            for invoice in invoices:
                status=await self.poll_invoice(dict(invoice),notify=False)
                if status=='paid':return True
                if status not in ('created','sent','expired'):return True
                checked.add(invoice['request_id'])
        except Exception:
            async with self.tx() as db:
                row=unpack(await db.fetchrow('SELECT * FROM bookings WHERE id=$1',ident))
                await self.enqueue(db,row,'reserve_check_failed','Не удалось проверить оплату перед автоматической отменой брони № '+ident+'. Бронь сохранена, проверка будет повторена.',self.settings.admin_ids)
            return True
        async with self.tx() as db:
            clock=await self.clock(db)
            row=unpack(await db.fetchrow('SELECT * FROM bookings WHERE id=$1 FOR UPDATE',ident))
            window=await db.fetchrow('SELECT * FROM booking_payment_windows WHERE booking_id=$1',ident)
            current=await db.fetch('SELECT request_id,state FROM paykeeper_invoices WHERE booking_id=$1',ident)
            if not row or row['status']!='pending' or row['paid'] or not window['warned_at'] or clock<window['warned_at']+timedelta(minutes=10):return True
            if {i['request_id'] for i in current}!=checked or any(i['state'] in ('queued','creating','uncertain','received') for i in current):return True
            if await db.fetchval('SELECT 1 FROM paykeeper_payments p JOIN paykeeper_invoices i USING(request_id) WHERE i.booking_id=$1 LIMIT 1',ident):return True
            reason='Истёк срок оплаты резерва.'
            await db.execute("UPDATE bookings SET status='cancelled',reason=$2,revision=revision+1 WHERE id=$1",ident,reason)
            await db.execute("UPDATE payment_requests SET state='superseded',revision=revision+1 WHERE booking_id=$1 AND state IN ('requested','linked','reported')",ident)
            await db.execute("UPDATE outbox SET disabled=true WHERE booking_id=$1 AND delivered_at IS NULL AND (event LIKE 'reminder:%' OR event LIKE 'paylink:%' OR event LIKE 'payreq:%' OR event LIKE 'payreported:%')",ident)
            await self.audit(db,ident,0,'reserve_cancelled',dict(reason=reason))
            await self.enqueue(db,row,'reserve_cancelled','Бронь на '+row['starts_at'].astimezone(TZ).strftime('%d.%m.%Y в %H:%M')+' отменена автоматически: оплата не поступила в срок. Вы можете выбрать свободное время в меню «Забронировать». Если вы уже оплатили, свяжитесь с администратором.')
        return True
