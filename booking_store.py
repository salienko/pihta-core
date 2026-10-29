"""Single-resource booking service. Every calendar/status mutation takes one DB lock."""
import hashlib
import json
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime,timedelta
from pathlib import Path
import asyncpg
from booking_domain import TZ,DEFAULT_RATES,RuleError,estimate,validate_start,can_cancel,card,clean_name,normalize_phone
from booking_flow import VERSION,require
from booking_guests import GuestStore
from booking_payments import PaymentStore,DEPOSIT,total as payment_total
from paykeeper_store import PayKeeperStore
from final_receipts import FinalReceiptStore
from booking_reserve import ReserveStore
from staff_booking import StaffBookingStore

SCHEMA='pihta_booking_v2'
LOCK=7319845
WORKER_LOCK=7319846


def start_state(data):
    """С какого шага начинать оформление.

    Имя и телефон, которые гость уже оставил (в «Моих данных» или в прошлой
    брони), повторно не спрашиваем — сразу переходим к числу гостей.
    """
    if data.get('name') and data.get('phone'):
        return 'guests'
    if data.get('name'):
        return 'phone'
    return 'name'


class WorkerLock:
    """Single-instance lock of the core.

    The advisory lock is session-scoped: it lives on one dedicated connection.
    If that connection dies (network drop, DB restart, idle timeout) the lock
    is released silently and a second core instance may start, sending the
    same notifications twice. `holds()` re-checks that this very session still
    owns the lock, so the caller can stop the background workers.
    """
    def __init__(self,pool,key=WORKER_LOCK):
        self.pool=pool
        self.key=key
        self.conn=None
        self.pid=None

    async def acquire(self):
        """Try to take the lock. Returns False if another instance owns it."""
        self.conn=await self.pool.acquire()
        try:
            taken=await self.conn.fetchval('SELECT pg_try_advisory_lock($1)',self.key)
        except BaseException:
            await self.pool.release(self.conn)
            self.conn=None
            raise
        if not taken:
            await self.pool.release(self.conn)
            self.conn=None
            return False
        self.pid=await self.conn.fetchval('SELECT pg_backend_pid()')
        return True

    async def holds(self):
        """True only if our own backend session still holds the advisory lock."""
        if self.conn is None or self.pid is None:
            return False
        try:
            pid=await self.conn.fetchval('SELECT pg_backend_pid()')
        except Exception:
            return False
        if pid!=self.pid:
            # The pooled connection was replaced: our session is gone and so is
            # the lock.
            return False
        try:
            row=await self.pool.fetchrow(
                'SELECT count(*) AS n FROM pg_locks WHERE locktype=$1 AND pid=$2 AND granted'
                ' AND classid::bigint=$3 AND objid::bigint=$4',
                'advisory',pid,self.key>>32,self.key&0xffffffff)
        except Exception:
            # Cannot prove the lock is alive: treat it as lost, the caller
            # stops the workers and the platform restarts the instance.
            return False
        return bool(row['n'])

    async def release(self):
        if self.conn is None:
            return
        try:
            await self.conn.fetchval('SELECT pg_advisory_unlock($1)',self.key)
        except Exception:
            pass
        try:
            await self.pool.release(self.conn)
        except Exception:
            pass
        self.conn=None
        self.pid=None

def unpack(row):
    if not row:return None
    d=dict(row)
    for key in ('data','estimate','detail','extras'):
        if key in d and isinstance(d[key],str):d[key]=json.loads(d[key])
    return d

class BookingStore(ReserveStore,StaffBookingStore,GuestStore,FinalReceiptStore,PayKeeperStore,PaymentStore):
    def __init__(self,settings,pool):self.settings,self.pool=settings,pool

    @classmethod
    async def connect(cls,settings,schema=SCHEMA):
        if not re.fullmatch('[a-z][a-z0-9_]{0,62}',schema):raise ValueError('Invalid schema')
        pool=await asyncpg.create_pool(settings.database_url,min_size=1,max_size=8,command_timeout=30,server_settings={'search_path':schema})
        try:
            async with pool.acquire() as db:
                async with db.transaction():
                    await db.execute('SELECT pg_advisory_xact_lock($1)',LOCK)
                    await db.execute(f'CREATE SCHEMA IF NOT EXISTS {schema}')
                    await db.execute(Path(__file__).with_name('booking_schema.sql').read_text(encoding='utf-8'))
        except BaseException:
            await pool.close();raise
        return cls(settings,pool)

    @asynccontextmanager
    async def tx(self):
        async with self.pool.acquire() as db:
            async with db.transaction():
                await db.execute('SELECT pg_advisory_xact_lock($1)',LOCK)
                yield db

    async def clock(self,db,clock=None):return clock or await db.fetchval('SELECT clock_timestamp()')
    def admin(self,actor):
        if actor not in self.settings.admin_ids:raise RuleError('Нет доступа.')
    async def audit(self,db,ident,actor,action,detail=None):
        await db.execute('INSERT INTO audit(booking_id,actor,action,detail) VALUES($1,$2,$3,$4::jsonb)',ident,actor,action,json.dumps(detail or {},ensure_ascii=False))
    async def enqueue(self,db,row,event,text,recipients=None):
        for who in set(recipients if recipients is not None else [row['telegram_id'],*self.settings.admin_ids]):
            if who is None:continue
            await db.execute('INSERT INTO outbox(booking_id,recipient,event,text) VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING',row['id'],who,event,text)

    async def expire(self,db,clock):
        rows=await db.fetch("SELECT * FROM bookings WHERE status IN ('pending','confirmed') AND ends_at<=$1 FOR UPDATE",clock)
        for raw in rows:
            row=unpack(raw);status='expired' if row['status']=='pending' and not row['paid'] else 'completed'
            reason='Сеанс закончился без подтверждения.' if status=='expired' else ''
            await db.execute('UPDATE bookings SET status=$1,reason=$2,revision=revision+1 WHERE id=$3',status,reason,row['id'])
            await db.execute("UPDATE outbox SET disabled=true WHERE booking_id=$1 AND event LIKE 'reminder:%' AND delivered_at IS NULL",row['id'])
            await self.audit(db,row['id'],0,status)
            if status=='expired':await self.enqueue(db,row,status,f"Заявка № {row['id']} отменена автоматически. Сеанс закончился без подтверждения. Запись сохранена в истории.")

    async def begin(self,actor):
        async with self.tx() as db:
            old=unpack(await db.fetchrow('SELECT * FROM drafts WHERE telegram_id=$1',actor))
            if old and old['state'] not in ('submitted','discarded'):return old['token']
            token=secrets.token_hex(6);known=await self.contact_card(db,actor)
            data={'flow_version':VERSION,'guests':1,'children':0,'hours':2,'cart':{}}
            if known:data.update(name=known['name'],phone=known['phone'])
            state=start_state(data)
            await db.execute("INSERT INTO drafts(telegram_id,token,state,data,revision,updated_at) VALUES($1,$2,$3,$4::jsonb,0,now()) ON CONFLICT(telegram_id) DO UPDATE SET token=$2,state=$3,data=$4::jsonb,revision=drafts.revision+1,updated_at=now()",actor,token,state,json.dumps(data))
            return token

    async def draft(self,actor,token=None):
        d=unpack(await self.pool.fetchrow('SELECT * FROM drafts WHERE telegram_id=$1',actor))
        if not d or (token and token!=d['token']) or d['state'] in ('submitted','discarded'):raise RuleError('Откройте новое оформление через «Забронировать».')
        return d

    async def save(self,actor,d,state,data):
        result=await self.pool.execute('UPDATE drafts SET state=$1,data=$2::jsonb,revision=revision+1,updated_at=now() WHERE telegram_id=$3 AND token=$4 AND revision=$5 AND state<>\'submitted\'',state,json.dumps(data,ensure_ascii=False),actor,d['token'],d['revision'])
        if result!='UPDATE 1':raise RuleError('Экран уже изменился. Откройте текущее оформление.')

    async def abandon_draft(self,actor,d):
        result=await self.pool.execute("UPDATE drafts SET state='discarded',revision=revision+1,updated_at=now() WHERE telegram_id=$1 AND token=$2 AND revision=$3 AND state NOT IN ('submitted','discarded')",actor,d['token'],d['revision'])
        if result!='UPDATE 1':raise RuleError('Черновик уже изменился.')

    async def reset_draft(self,actor,d):
        async with self.tx() as db:
            row=unpack(await db.fetchrow('SELECT * FROM drafts WHERE telegram_id=$1 FOR UPDATE',actor))
            if not row or row['token']!=d['token'] or row['revision']!=d['revision'] or row['state']=='submitted':
                raise RuleError('Черновик уже изменился. Продолжите с актуального шага.')
            known=await self.contact_card(db,actor)
            data={'flow_version':VERSION,'guests':1,'children':0,'hours':2,'cart':{}}
            if known:data.update(name=known['name'],phone=known['phone'])
            await db.execute("UPDATE drafts SET token=$1,state=$2,data=$3::jsonb,revision=revision+1,updated_at=now() WHERE telegram_id=$4",secrets.token_hex(6),start_state(data),json.dumps(data,ensure_ascii=False),actor)

    async def profile(self,actor,name=None,phone=None):
        """«Мои данные»: у склеенного гостя имя и номер берём из записи гостя."""
        if name is None:
            guest=await self.guest_of(self.pool,actor)
            if guest:return dict(name=guest['name'],phone=guest['phone'])
            return unpack(await self.pool.fetchrow('SELECT * FROM profiles WHERE telegram_id=$1',actor))
        await self.pool.execute('INSERT INTO profiles VALUES($1,$2,$3) ON CONFLICT(telegram_id) DO UPDATE SET name=$2,phone=$3',actor,clean_name(name),normalize_phone(phone))

    async def seed_prices(self,db):
        """Первый запуск: цены услуг берём из content.json, тарифы аренды — из DEFAULT_RATES."""
        for code,amount in DEFAULT_RATES.items():
            await db.execute('INSERT INTO price_settings(code,amount) VALUES($1,$2) ON CONFLICT(code) DO NOTHING',code,amount)
        for order,item in enumerate(self.settings.content['services']):
            await db.execute('INSERT INTO service_settings(code,name,price,unit,available,sort_order) VALUES($1,$2,$3,$4,true,$5) ON CONFLICT(code) DO NOTHING',item['code'],item['name'],item['price'],item.get('unit','шт.'),order)
            # Строка могла остаться от версии, где в таблице хранилась только доступность.
            await db.execute('UPDATE service_settings SET name=COALESCE(name,$2),price=COALESCE(price,$3),unit=COALESCE(unit,$4),sort_order=$5 WHERE code=$1',item['code'],item['name'],item['price'],item.get('unit','шт.'),order)

    async def pricing(self,db=None):
        """Каталог услуг, праздники и тарифы аренды. Цены читаются из БД, content.json — только сид."""
        db=db or self.pool
        if not await db.fetchval('SELECT EXISTS(SELECT 1 FROM service_settings) AND EXISTS(SELECT 1 FROM price_settings)'):
            await self.seed_prices(db)
        catalog={r['code']:dict(code=r['code'],name=r['name'],price=r['price'],unit=r['unit'],available=r['available'])
                 for r in await db.fetch('SELECT * FROM service_settings WHERE price IS NOT NULL ORDER BY sort_order,code')}
        rates=dict(DEFAULT_RATES,**{r['code']:r['amount'] for r in await db.fetch('SELECT code,amount FROM price_settings')})
        holidays=dict(self.settings.content.get('holidays',{}))
        for r in await db.fetch('SELECT * FROM holiday_rates'):holidays[r['day'].isoformat()]=dict(label=r['label'],base=r['base'],extra=r['extra'])
        return catalog,holidays,rates

    async def service(self,code):
        return await self.pool.fetchrow('SELECT * FROM service_settings WHERE code=$1',code)

    async def add_service(self,actor,code,name,price,unit='шт.'):
        """Новая услуга в ассортименте: админ задаёт код, название и цену."""
        if not re.fullmatch('[a-z][a-z0-9_]{1,19}',code):
            raise RuleError('Код услуги: латинские буквы, цифры и подчёркивание, от 2 до 20 символов.')
        async with self.tx() as db:
            if await db.fetchval('SELECT 1 FROM service_settings WHERE code=$1',code):
                raise RuleError('Услуга с таким кодом уже есть.')
            order=await db.fetchval('SELECT COALESCE(max(sort_order),0)+1 FROM service_settings')
            await db.execute('INSERT INTO service_settings(code,name,price,unit,available,sort_order) '
                             'VALUES($1,$2,$3,$4,true,$5)',code,name,price,unit,order)
            await self.audit(db,None,actor,'service_added',dict(code=code,name=name,price=price))

    async def set_service(self,actor,code,name,price,available):
        """Правка услуги из админки. Снимки существующих броней не меняются."""
        self.admin(actor)
        if not 1<=len(name)<=80:raise RuleError('Укажите название услуги до 80 символов.')
        if type(price) is not int or not 0<price<=100000000:raise RuleError('Цена должна быть целым числом больше нуля.')
        async with self.tx() as db:
            if await db.execute('UPDATE service_settings SET name=$2,price=$3,available=$4,updated_at=now() WHERE code=$1',code,name,price,available)!='UPDATE 1':
                raise RuleError('Услуга не найдена. Обновите страницу.')
            await self.audit(db,None,actor,'service_price',dict(code=code,name=name,price=price,available=available))

    async def set_rate(self,actor,code,amount):
        """Правка тарифа аренды или доплаты за гостя. Только для новых броней."""
        self.admin(actor)
        if code not in DEFAULT_RATES:raise RuleError('Неизвестный тариф.')
        if type(amount) is not int or not 0<amount<=100000000:raise RuleError('Цена должна быть целым числом больше нуля.')
        if code!='guest_extra' and amount%2:raise RuleError('Цена за 2 часа должна делиться на 2.')
        async with self.tx() as db:
            await db.execute('INSERT INTO price_settings(code,amount,updated_at) VALUES($1,$2,now()) ON CONFLICT(code) DO UPDATE SET amount=EXCLUDED.amount,updated_at=now()',code,amount)
            await self.audit(db,None,actor,'rate_price',dict(code=code,amount=amount))

    async def blocked(self,db,start,end):
        return await db.fetchval("""SELECT EXISTS(SELECT 1 FROM bookings WHERE
          (status IN ('pending','confirmed','completed') OR (status='expired' AND blocked_until>clock_timestamp()))
          AND starts_at<$2 AND blocked_until>$1) OR EXISTS(SELECT 1 FROM calendar_blocks WHERE active AND starts_at<$2 AND ends_at>$1)""",start,end)

    async def free(self,start,clock=None):
        async with self.tx() as db:
            clock=await self.clock(db,clock);await self.expire(db,clock)
            end=validate_start(start,clock)
            return not await self.blocked(db,start,end+timedelta(hours=1))

    async def available_days(self,days):
        from booking_domain import SLOTS
        async with self.tx() as db:
            clock=await self.clock(db);await self.expire(db,clock)
            ranges=await db.fetch("SELECT starts_at,blocked_until AS ends_at FROM bookings WHERE status IN ('pending','confirmed','completed') OR (status='expired' AND blocked_until>clock_timestamp()) UNION ALL SELECT starts_at,ends_at FROM calendar_blocks WHERE active")
            result=set()
            for day in days:
                for hour in SLOTS:
                    start=datetime.combine(day,datetime.min.time(),TZ)+timedelta(hours=hour)
                    try:end=validate_start(start,clock)+timedelta(hours=1)
                    except RuleError:continue
                    if not any(r['starts_at']<end and r['ends_at']>start for r in ranges):
                        result.add(day);break
            return result

    async def submit(self,actor,token,clock=None,expected_revision=None,accept_rules=False):
        async with self.tx() as db:
            clock=await self.clock(db,clock);await self.expire(db,clock)
            old=await db.fetchval('SELECT id FROM bookings WHERE telegram_id=$1 AND draft_token=$2',actor,token)
            if old:return old,False
            d=unpack(await db.fetchrow('SELECT * FROM drafts WHERE telegram_id=$1 FOR UPDATE',actor))
            if not d or d['token']!=token or d['state']!='review':raise RuleError('Откройте итог текущего черновика.')
            data=d['data']
            if expected_revision is not None and d['revision']!=expected_revision:raise RuleError('Экран изменился. Проверьте итог заново.')
            if data.get('flow_version')!=VERSION:raise RuleError('Продолжите оформление в новой версии бота.')
            require(data,'review')
            if data.get('cart'):
                raise RuleError('Эта версия принимает только аренду. Откройте итог бронирования заново.')
            start=datetime.fromisoformat(data['start']);end=validate_start(start,clock)
            if accept_rules:
                data['consent']=True
                data['guest_min_paid_age']=8
            if not data.get('consent') or data.get('rules_hash')!=self.settings.rules_hash:raise RuleError('Примите актуальные правила.')
            data['name']=clean_name(data['name']);data['phone']=normalize_phone(data['phone'])
            catalog,holidays,rates=await self.pricing(db)
            q=estimate(data,catalog,holidays,rates)
            if q!=data.get('quote'):raise RuleError('Расчёт изменился. Проверьте итог и примите правила заново.')
            rows=await db.fetch("SELECT starts_at FROM bookings WHERE telegram_id=ANY($1::bigint[]) AND status IN ('pending','confirmed') AND ends_at>$2",(await self.account_ids(db,actor))[0],clock)
            if len(rows)>=2:raise RuleError('У вас уже две активные брони. Откройте «Мои бронирования».')
            if any(r['starts_at'].astimezone(TZ).date()==start.astimezone(TZ).date() for r in rows):raise RuleError('На эту дату у вас уже есть бронь. Выберите другую дату.')
            if await self.blocked(db,start,end+timedelta(hours=1)):raise RuleError('Это время уже занято. Выберите другой сеанс.')
            ident=secrets.token_hex(5).upper()
            await db.execute("INSERT INTO bookings(id,draft_token,telegram_id,starts_at,ends_at,blocked_until,data,estimate,rules_hash,created_at,status) VALUES($1,$2,$3,$4,$5,$6,$7::jsonb,$8::jsonb,$9,$10,'pending')",ident,token,actor,start,end,end+timedelta(hours=1),json.dumps(data,ensure_ascii=False),json.dumps(q,ensure_ascii=False),self.settings.rules_hash,clock)
            await db.execute('INSERT INTO profiles VALUES($1,$2,$3) ON CONFLICT(telegram_id) DO UPDATE SET name=$2,phone=$3',actor,data['name'],data['phone'])
            # Имя гостя дополняем, номер не трогаем: он меняется только подтверждением.
            account=await self.account_of(db,actor)
            if account:await db.execute("UPDATE guests SET name=CASE WHEN name='' THEN $2 ELSE name END,updated_at=now() WHERE id=$1",account['guest_id'],data['name'])
            await db.execute("UPDATE drafts SET state='submitted',revision=revision+1,updated_at=now() WHERE telegram_id=$1",actor)
            row=unpack(await db.fetchrow('SELECT * FROM bookings WHERE id=$1',ident))
            await self.audit(db,ident,actor,'submitted');await self.enqueue(db,row,'submitted',card(row))
            return ident,True

    async def get(self,actor,ident,staff=False):
        if staff:self.admin(actor)
        async with self.tx() as db:
            await self.expire(db,await self.clock(db))
            r=unpack(await db.fetchrow(
                'SELECT b.*, c.phone AS contact_phone FROM bookings b '
                'LEFT JOIN booking_contacts c ON c.booking_id=b.id WHERE b.id=$1',ident))
            if not r or (not staff and not await self.owns(db,actor,r['telegram_id'])):raise RuleError('Бронь недоступна.')
            # снимок брони неизменяем, поэтому исправленный телефон хранится рядом
            r['contact_phone']=r.get('contact_phone') or r['data'].get('phone','')
            return r

    async def set_contact_phone(self,ident,phone,actor):
        async with self.tx() as db:
            await db.execute('INSERT INTO booking_contacts(booking_id,phone,updated_by,updated_at) '
                             'VALUES($1,$2,$3,now()) ON CONFLICT(booking_id) DO UPDATE '
                             'SET phone=EXCLUDED.phone,updated_by=EXCLUDED.updated_by,updated_at=now()',
                             ident,phone,actor)
            await self.audit(db,ident,actor,'phone_changed',{'phone':phone})

    async def set_booking_services(self,actor,ident,cart):
        """Гость меняет доп. услуги в своей брони: состав и количество.

        Снимок брони (data, estimate) неизменяем, поэтому дополнительный набор
        лежит в отдельных полях extras/extras_total. Менять можно, пока сеанс не
        закончился; сумма не может опуститься ниже уже внесённых денег.
        """
        async with self.tx() as db:
            await self.expire(db,await self.clock(db))
            r=unpack(await db.fetchrow('SELECT * FROM bookings WHERE id=$1 FOR UPDATE',ident))
            if not r or not await self.owns(db,actor,r['telegram_id']):raise RuleError('Бронь недоступна.')
            if r['status'] not in ('pending','confirmed') or r['no_show']:
                raise RuleError('Доп. услуги доступны только в действующей брони.')
            if r['ends_at']<=await self.clock(db):
                raise RuleError('Сеанс закончился: услуги уже не изменить.')
            catalog,_,_=await self.pricing(db)
            extras={}
            for code,qty in cart.items():
                qty=int(qty)
                if qty<=0:continue
                item=catalog.get(code)
                if not item or not item.get('available',True):
                    raise RuleError('Услуга недоступна. Откройте список услуг заново.')
                if qty>100:raise RuleError('Не более 100 единиц одной услуги.')
                extras[code]=dict(qty=qty,price=item['price'],name=item['name'],
                                  unit=item.get('unit') or 'шт.')
            amount=sum(e['price']*e['qty'] for e in extras.values())
            if r['estimate']['total']+amount<r['paid']:
                raise RuleError('Сумма станет меньше внесённой оплаты. Убрать оплаченные услуги может администратор.')
            await db.execute('UPDATE bookings SET extras=$2::jsonb,extras_total=$3,revision=revision+1 WHERE id=$1',
                             ident,json.dumps(extras,ensure_ascii=False),amount)
            await self.audit(db,ident,actor,'services_changed',
                             {'extras':extras,'total':r['estimate']['total']+amount})
            return amount

    async def pending_input(self,actor):
        return await self.pool.fetchrow(
            'SELECT * FROM pending_inputs WHERE actor=$1 AND expires_at>now()',actor)

    async def set_pending_input(self,actor,kind,ref=None,minutes=30):
        await self.pool.execute(
            "INSERT INTO pending_inputs(actor,kind,ref,expires_at) VALUES($1,$2,$3,now()+$4*interval '1 minute') "
            'ON CONFLICT(actor) DO UPDATE SET kind=EXCLUDED.kind,ref=EXCLUDED.ref,'
            'expires_at=EXCLUDED.expires_at,created_at=now()',actor,kind,ref,float(minutes))

    async def clear_pending_input(self,actor):
        await self.pool.execute('DELETE FROM pending_inputs WHERE actor=$1',actor)

    async def listing(self,actor,history=False,staff=False,offset=0,status_filter=None):
        if staff:self.admin(actor)
        if status_filter not in (None,'pending','confirmed','cancelled','rejected','expired','completed'):raise RuleError('Выберите статус из списка.')
        async with self.tx() as db:
            clock=await self.clock(db);await self.expire(db,clock)
            # Брони гостя — по всем его аккаунтам, а не по тому, откуда он пишет сейчас.
            ids=(await self.account_ids(db,actor))[0] if not staff else [actor]
            rows=await db.fetch("SELECT * FROM bookings WHERE ($1::boolean OR telegram_id=ANY($2::bigint[])) AND ($3::boolean OR ((status IN ('pending','confirmed')) <> $4::boolean)) AND ($6::text IS NULL OR status=$6) ORDER BY starts_at DESC,id LIMIT 21 OFFSET $5",staff,ids,staff,history,max(0,offset),status_filter)
            return [unpack(r) for r in rows]

    async def legacy(self,actor,ident=None):
        if not await self.pool.fetchval("SELECT to_regclass('pihta_intake_v1.orders') IS NOT NULL"):return [] if ident is None else None
        if ident is None:return [unpack(r) for r in await self.pool.fetch('SELECT * FROM pihta_intake_v1.orders WHERE telegram_id=$1 ORDER BY created_at DESC LIMIT 20',actor)]
        return unpack(await self.pool.fetchrow('SELECT * FROM pihta_intake_v1.orders WHERE telegram_id=$1 AND id=$2',actor,ident))

    async def transition(self,actor,ident,action,reason='',clock=None,staff=False):
        if staff:self.admin(actor)
        async with self.tx() as db:
            clock=await self.clock(db,clock);await self.expire(db,clock)
            row=unpack(await db.fetchrow('SELECT * FROM bookings WHERE id=$1 FOR UPDATE',ident))
            if not row or (not staff and not await self.owns(db,actor,row['telegram_id'])):raise RuleError('Бронь недоступна.')
            if action not in ('confirm','reject','cancel'):raise RuleError('Неизвестное действие.')
            target={'confirm':'confirmed','reject':'rejected','cancel':'cancelled'}[action]
            if row['status']==target:return
            if row['status'] not in ('pending','confirmed'):raise RuleError('Заявка уже закрыта.')
            if action in ('confirm','reject') and (not staff or row['status']!='pending'):raise RuleError('Действие недоступно.')
            if action=='confirm' and row['paid']<min(DEPOSIT,payment_total(row)):raise RuleError('Сначала подтвердите поступление предоплаты или полной стоимости.')
            if not staff and row['paid']:raise RuleError('Оплаченную бронь отменяет только администратор. Свяжитесь с ним для расчёта и возврата.')
            if not staff and row['status']=='confirmed':raise RuleError('Подтверждённую бронь отменяет администратор. Отправьте запрос на отмену.')
            if not staff and (action!='cancel' or not can_cancel(row['status'],row['starts_at'],clock)):raise RuleError('До начала менее 24 часов. Свяжитесь с администратором.')
            if staff and action!='confirm' and not 2<=len(reason.strip())<=500:raise RuleError('Укажите причину от 2 до 500 символов.')
            await db.execute('UPDATE bookings SET status=$1,reason=$2,revision=revision+1 WHERE id=$3',target,reason.strip(),ident)
            await db.execute("UPDATE outbox SET disabled=true WHERE booking_id=$1 AND event LIKE 'reminder:%' AND delivered_at IS NULL",ident)
            if target in ('cancelled','rejected'):
                await db.execute("UPDATE payment_requests SET state='superseded',revision=revision+1 WHERE booking_id=$1 AND state IN ('requested','linked','reported')",ident)
                await db.execute("UPDATE outbox SET disabled=true WHERE booking_id=$1 AND delivered_at IS NULL AND (event LIKE 'payreq:%' OR event LIKE 'paylink:%' OR event LIKE 'payreported:%')",ident)
            row.update(status=target,reason=reason.strip())
            await self.audit(db,ident,actor,action,{'reason':reason.strip()});await self.enqueue(db,row,'status:'+target,card(row))

    async def accounting(self,actor,ident,key,kind,amount=0,guests=None,total=None,note=''):
        self.admin(actor)
        if kind not in ('payment','refund','actual','no_show'):raise RuleError('Неизвестное действие.')
        if not 2<=len(note.strip())<=500:raise RuleError('Укажите комментарий от 2 до 500 символов.')
        async with self.tx() as db:
            if await db.fetchval('SELECT key FROM operations WHERE key=$1',key):return
            row=unpack(await db.fetchrow('SELECT * FROM bookings WHERE id=$1 FOR UPDATE',ident))
            if not row:raise RuleError('Бронь не найдена.')
            if await db.fetchval('SELECT 1 FROM paykeeper_settlements WHERE booking_id=$1',ident):
                raise RuleError('Итоговый чек уже запрошен. Изменение расчёта требует отдельной сверки и кассовой процедуры.')
            if kind in ('payment','refund'):
                if type(amount) is not int or not 0<amount<=100000000:raise RuleError('Проверьте сумму.')
                paid=row['paid']+(amount if kind=='payment' else -amount)
                if paid<0:raise RuleError('Возврат больше фактически внесённой суммы.')
                await db.execute('UPDATE bookings SET paid=$1,revision=revision+1 WHERE id=$2',paid,ident)
                if kind=='payment' and row['status']=='pending' and paid>=min(DEPOSIT,payment_total(row)):
                    await db.execute("UPDATE bookings SET status='confirmed' WHERE id=$1",ident)
                await db.execute("UPDATE payment_requests SET state='superseded',revision=revision+1 WHERE booking_id=$1 AND state IN ('requested','linked','reported')",ident)
                await db.execute("UPDATE outbox SET disabled=true WHERE booking_id=$1 AND delivered_at IS NULL AND (event LIKE 'payreq:%' OR event LIKE 'paylink:%' OR event LIKE 'payreported:%')",ident)
            elif kind=='actual':
                if type(guests) is not int or not 1<=guests<=8 or type(total) is not int or total<row['estimate']['total']+int(row.get('extras_total') or 0):raise RuleError('1–8 гостей; сумма не меньше суммы исходной брони.')
                await db.execute('UPDATE bookings SET actual_guests=$1,actual_total=$2,revision=revision+1 WHERE id=$3',guests,total,ident)
            else:
                if row['starts_at']>await self.clock(db) or row['status'] not in ('confirmed','completed'):raise RuleError('Неявка отмечается после начала подтверждённого сеанса.')
                await db.execute('UPDATE bookings SET no_show=true,revision=revision+1 WHERE id=$1',ident)
            await self.audit(db,ident,actor,kind,dict(amount=amount,guests=guests,total=total,note=note,payment_kind=('deposit' if not row['paid'] and amount==DEPOSIT else 'full' if not row['paid'] and amount>=payment_total(row) else 'balance' if row['paid'] else 'partial') if kind=='payment' else None))
            await db.execute('INSERT INTO operations VALUES($1,$2,$3)',key,actor,ident)
            updated=unpack(await db.fetchrow('SELECT * FROM bookings WHERE id=$1',ident))
            await self.enqueue(db,updated,'account:'+key,card(updated),[row['telegram_id']])

    async def staff_note(self,actor,ident,key,text):
        self.admin(actor)
        if not 2<=len(text.strip())<=1000:raise RuleError('Сообщение от 2 до 1000 символов.')
        async with self.tx() as db:
            row=unpack(await db.fetchrow('SELECT * FROM bookings WHERE id=$1',ident))
            if not row:raise RuleError('Бронь не найдена.')
            if await db.fetchval('SELECT key FROM operations WHERE key=$1',key):return
            await self.enqueue(db,row,'note:'+key,f"По брони № {ident}:\n{text.strip()}",[row['telegram_id']])
            await self.audit(db,ident,actor,'message',{'text':text.strip()})
            await db.execute('INSERT INTO operations VALUES($1,$2,$3)',key,actor,ident)

    async def tick(self):
        async with self.tx() as db:
            clock=await self.clock(db);await self.expire(db,clock)
            rows=await db.fetch("SELECT * FROM bookings WHERE status IN ('pending','confirmed') AND ends_at>$1",clock)
            for raw in rows:
                row=unpack(raw);start=row['starts_at'];age=(clock-row['created_at']).total_seconds()
                if row['status']=='pending':
                    await self.reserve_reminders(db,row,clock)
                    if age>=900:
                        n=int((age-900)//3600)
                        await self.enqueue(db,row,f'reminder:pending:{n}',f"Заявка № {row['id']} ждёт подтверждения.\n"+card(row),self.settings.admin_ids)
                    if timedelta(0)<start-clock<=timedelta(hours=1):
                        # Same key as the periodic reminder when both deadlines coincide.
                        event=f'reminder:pending:{int((age-900)//3600)}' if age>=900 else 'reminder:pending:soon'
                        await self.enqueue(db,row,event,f"До начала заявки № {row['id']} не более часа. Требуется решение.",self.settings.admin_ids)
                else:
                    for hours in (24,1):
                        deadline=start-timedelta(hours=hours)
                        if row['created_at']<=deadline<=clock<deadline+timedelta(minutes=2):
                            await self.enqueue(db,row,f'reminder:confirmed:{hours}',f"Напоминание: сеанс № {row['id']} {start.astimezone(TZ):%d.%m %H:%M}. Остаток оплаты, если есть, внесите до начала сеанса.",[row['telegram_id']])
            await db.execute('DELETE FROM web_tokens WHERE expires_at<$1',clock)
            await db.execute('DELETE FROM web_sessions WHERE expires_at<$1',clock)

    async def cleanup_old_data(self,clock=None):
        """Ежедневная уборка старых данных. Брони и платежи не трогаем."""
        async with self.pool.acquire() as db:
            async with db.transaction():
                now=clock or await db.fetchval('SELECT clock_timestamp()')
                counts={}
                has_column=await db.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema=current_schema() AND table_name='drafts' AND column_name='updated_at')")
                if has_column:
                    counts['drafts']=await db.execute(
                        'DELETE FROM drafts WHERE updated_at<$1',now-timedelta(days=2))
                else:
                    counts['drafts']='SKIP'
                counts['outbox']=await db.execute(
                    'DELETE FROM outbox WHERE delivered_at IS NOT NULL AND delivered_at<$1',now-timedelta(days=30))
                counts['audit']=await db.execute(
                    'DELETE FROM audit WHERE created_at<$1',now-timedelta(days=365))
                if await db.fetchval("SELECT to_regclass('pending_inputs') IS NOT NULL"):
                    counts['pending_inputs']=await db.execute(
                        'DELETE FROM pending_inputs WHERE expires_at<$1',now)
                else:
                    counts['pending_inputs']='SKIP'
                return {k:(int(v.split()[-1]) if v.startswith('DELETE') else v) for k,v in counts.items()}

    async def next_delivery(self):
        async with self.pool.acquire() as db:
            async with db.transaction():
                row=await db.fetchrow('SELECT * FROM outbox WHERE NOT disabled AND delivered_at IS NULL AND due_at<=now() AND (locked_until IS NULL OR locked_until<now()) ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1')
                if not row:return None
                await db.execute("UPDATE outbox SET locked_until=now()+interval '2 minutes',attempts=attempts+1 WHERE id=$1",row['id']);return dict(row)
    async def delivered(self,ident):await self.pool.execute('UPDATE outbox SET delivered_at=now(),locked_until=NULL WHERE id=$1',ident)
    async def failed(self,ident,code,retry=60,permanent=False):await self.pool.execute("UPDATE outbox SET last_error=$2,disabled=$3,locked_until=NULL,due_at=now()+$4*interval '1 second' WHERE id=$1",ident,code,permanent,float(max(5,retry)))

    async def login_token(self,actor):
        self.admin(actor);token=secrets.token_urlsafe(32)
        await self.pool.execute("INSERT INTO web_tokens VALUES($1,$2,now()+interval '5 minutes',false)",hashlib.sha256(token.encode()).hexdigest(),actor)
        return token
    async def login(self,token):
        if not isinstance(token,str) or len(token)>100:raise RuleError('Ссылка недействительна.')
        async with self.tx() as db:
            row=await db.fetchrow('UPDATE web_tokens SET used=true WHERE hash=$1 AND NOT used AND expires_at>now() RETURNING actor',hashlib.sha256(token.encode()).hexdigest())
            if not row:raise RuleError('Ссылка использована или истекла. Получите новую через /admin в боте.')
            self.admin(row['actor']);session=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(24)
            await db.execute("INSERT INTO web_sessions VALUES($1,$2,$3,now()+interval '8 hours')",hashlib.sha256(session.encode()).hexdigest(),row['actor'],csrf)
            return session
    async def session(self,token):
        if not token or len(token)>100:return None
        row=await self.pool.fetchrow('SELECT * FROM web_sessions WHERE hash=$1 AND expires_at>now()',hashlib.sha256(token.encode()).hexdigest())
        if row and row['actor'] in self.settings.admin_ids:return dict(row)
        return None
