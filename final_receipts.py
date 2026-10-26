"""Booking-level settlement receipts, separate from incoming money."""
import json
import secrets
from booking_domain import RuleError
from booking_payments import total


class FinalReceiptStore:
    async def request_final_receipt(self, actor, ident):
        self.admin(actor)
        if not self.settings.paykeeper_settlement_enabled:raise RuleError('Итоговые чеки временно отключены.')
        if not self.settings.paykeeper_enabled:raise RuleError('Приём оплаты пока не включён.')
        from booking_store import unpack
        async with self.tx() as db:
            old=await db.fetchrow('SELECT * FROM paykeeper_settlements WHERE booking_id=$1',ident)
            if old:return dict(old)
            row=unpack(await db.fetchrow('SELECT * FROM bookings WHERE id=$1 FOR UPDATE',ident))
            if not row or row['status'] not in ('confirmed','completed') or row['no_show'] or row['ends_at']>await self.clock(db):
                raise RuleError('Подтвердить оказание услуги можно после окончания сеанса, без отмены и неявки.')
            payments=await db.fetch('SELECT p.*,i.payment_type FROM paykeeper_payments p JOIN paykeeper_invoices i USING(request_id) WHERE i.booking_id=$1',ident)
            amount=total(row)
            if not payments or row['paid']!=amount or sum(p['amount'] for p in payments)!=amount or any(p['disposition']!='applied' or p['payment_type'] not in ('prepay','part_prepay') for p in payments):
                raise RuleError('Нужна полная оплата подтверждёнными предоплатами через платёжный сервис. Ручные платежи, переплаты и спорные операции требуют сверки.')
            if await db.fetchval("SELECT 1 FROM audit WHERE booking_id=$1 AND action='refund' LIMIT 1",ident):
                raise RuleError('По брони был возврат: требуется отдельная сверка.')
            ids=[p['payment_id'] for p in payments]
            for p in payments:
                if not await db.fetchval('SELECT 1 FROM paykeeper_fiscal_receipts WHERE payment_id=$1',p['payment_id']):
                    raise RuleError('Дождитесь чеков всех предоплат.')
            result=await db.fetchrow('INSERT INTO paykeeper_settlements(booking_id,actor,amount,phone,tax,receipt_key,payment_ids) VALUES($1,$2,$3,$4,$5,$6,$7::jsonb) RETURNING *',ident,actor,amount,row['data']['phone'],self.settings.paykeeper_tax,secrets.token_urlsafe(24),json.dumps(ids))
            await self.audit(db,ident,actor,'settlement_requested',dict(amount=amount,payment_ids=ids))
            return dict(result)

    async def settlement_once(self):
        if not self.settings.paykeeper_enabled or not self.settings.paykeeper_settlement_enabled:return False
        async with self.tx() as db:
            # A crash after claiming must never silently reissue a fiscal document.
            stale=await db.fetch("UPDATE paykeeper_settlements SET state='uncertain',last_error='interrupted_submission' WHERE state='sending' AND next_check_at<now() RETURNING booking_id")
            for old in stale:
                from booking_store import unpack
                row=unpack(await db.fetchrow('SELECT * FROM bookings WHERE id=$1',old['booking_id']))
                await self.enqueue(db,row,'pkfinalerror:'+old['booking_id'],'⚠️ Отправка итогового чека была прервана. Требуется сверка с платёжным сервисом, повторная выдача остановлена.',self.settings.admin_ids)
            r=await db.fetchrow("SELECT * FROM paykeeper_settlements WHERE state IN ('queued','pending') AND next_check_at<=now() ORDER BY next_check_at LIMIT 1 FOR UPDATE")
            if not r:return False
            await db.execute("UPDATE paykeeper_settlements SET state=CASE WHEN state='queued' THEN 'sending' ELSE state END,next_check_at=now()+interval '2 minutes' WHERE booking_id=$1",r['booking_id'])
        try:
            if r['state']=='queued':
                ident=await self.provider().create_settlement(dict(r))
                async with self.tx() as db:
                    await db.execute("UPDATE paykeeper_settlements SET state='pending',receipt_id=$2 WHERE booking_id=$1",r['booking_id'],ident)
            else:
                result=await self.provider().settlement_status(dict(r))
                if result['state']=='pending':return True
                async with self.tx() as db:
                    await db.execute('UPDATE paykeeper_settlements SET state=$2,url=$3 WHERE booking_id=$1',r['booking_id'],result['state'],result.get('url',''))
                    from booking_store import unpack
                    row=unpack(await db.fetchrow('SELECT * FROM bookings WHERE id=$1',r['booking_id']))
                    if result['state']=='success':
                        await self.enqueue(db,row,'pkfinal:'+r['booking_id'],'Услуга оказана. Итоговый чек доступен по кнопке ниже. Повторная оплата не требуется.',[row['telegram_id']])
                    await self.enqueue(db,row,'pkfinalstaff:'+r['booking_id'],'Итоговый чек: '+('сформирован.' if result['state']=='success' else 'ошибка кассы, требуется проверка.'),self.settings.admin_ids)
                    await self.audit(db,r['booking_id'],0,'settlement_'+result['state'])
        except Exception:
            async with self.tx() as db:
                await db.execute("UPDATE paykeeper_settlements SET state=CASE WHEN receipt_id IS NULL THEN 'uncertain' ELSE state END,last_error='provider_check_required' WHERE booking_id=$1",r['booking_id'])
                from booking_store import unpack
                row=unpack(await db.fetchrow('SELECT * FROM bookings WHERE id=$1',r['booking_id']))
                await self.enqueue(db,row,'pkfinalerror:'+r['booking_id'],'⚠️ Итоговый чек требует сверки с платёжным сервисом. Не создавайте повторный чек с новым ключом.',self.settings.admin_ids)
        return True

    async def reconcile_settlement(self,actor,ident,receipt_id):
        self.admin(actor)
        if not self.settings.paykeeper_settlement_enabled:raise RuleError('Итоговые чеки временно отключены.')
        r=await self.pool.fetchrow("SELECT * FROM paykeeper_settlements WHERE booking_id=$1 AND state='uncertain'",ident)
        if not r:raise RuleError('Чек не требует сверки.')
        item=dict(r);item['receipt_id']=receipt_id
        try:await self.provider().settlement_status(item)
        except Exception:raise RuleError('Чек не прошёл проверку ключа, суммы и содержимого.') from None
        async with self.tx() as db:
            await db.execute("UPDATE paykeeper_settlements SET receipt_id=$2,state='pending',next_check_at=now() WHERE booking_id=$1 AND state='uncertain'",ident,receipt_id)
            await self.audit(db,ident,actor,'settlement_reconciled',dict(receipt_id=receipt_id))
