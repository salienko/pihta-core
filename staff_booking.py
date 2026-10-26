"""Staff-created telephone bookings; phone alone never assigns Telegram ownership."""
import json
import re
import secrets
from datetime import datetime,timedelta
from booking_domain import TZ,RuleError,validate_start,estimate,clean_name,normalize_phone,card


class StaffBookingStore:
    async def staff_quote(self,actor,start,phone,guests,children=0,name='',db=None):
        self.admin(actor)
        if not isinstance(start,datetime) or start.tzinfo is None:raise RuleError('Выберите дату и свободный сеанс.')
        clock=await self.clock(db) if db else datetime.now(TZ)
        validate_start(start,clock)
        if type(guests) is not int or type(children) is not int:raise RuleError('Укажите число гостей и детей.')
        data=dict(name=clean_name(name.strip()) if name.strip() else 'Гость',phone=normalize_phone(phone),guests=guests,children=children,hours=2,start=start.isoformat(),day=start.astimezone(TZ).date().isoformat(),cart={},guest_min_paid_age=8,source='staff',consent=False)
        catalog,holidays=await self.pricing(db)
        return data,estimate(data,catalog,holidays)

    async def create_staff_booking(self,actor,key,start,phone,guests,children=0,name='',expected_total=None):
        self.admin(actor)
        if not re.fullmatch('[a-f0-9]{32}',key):raise RuleError('Обновите форму.')
        from booking_store import unpack
        async with self.tx() as db:
            existing=unpack(await db.fetchrow('SELECT * FROM bookings WHERE draft_token=$1','staff:'+key))
            if existing:
                if (existing['starts_at'],existing['data']['phone'],existing['data']['guests'],existing['data']['children'],existing['data']['name'])!=(start,normalize_phone(phone),guests,children,clean_name(name) if name.strip() else 'Гость'):raise RuleError('Форма уже использована с другими данными.')
                return existing['id'],False
            clock=await self.clock(db);await self.expire(db,clock)
            data,quote=await self.staff_quote(actor,start,phone,guests,children,name,db=db)
            if quote['total']!=expected_total:raise RuleError('Стоимость изменилась. Рассчитайте бронь заново.')
            end=validate_start(start,clock)
            if await self.blocked(db,start,end+timedelta(hours=1)):raise RuleError('Это время уже занято. Выберите другой сеанс.')
            active=await db.fetch("SELECT starts_at FROM bookings WHERE data->>'phone'=$1 AND status IN ('pending','confirmed') AND ends_at>$2",data['phone'],clock)
            if len(active)>=2 or any(r['starts_at'].astimezone(TZ).date()==start.astimezone(TZ).date() for r in active):raise RuleError('У гостя уже есть бронь на эту дату или две действующие брони.')
            ident=secrets.token_hex(5).upper()
            await db.execute("INSERT INTO bookings(id,draft_token,telegram_id,starts_at,ends_at,blocked_until,data,estimate,rules_hash,created_at,status) VALUES($1,$2,NULL,$3,$4,$5,$6::jsonb,$7::jsonb,$8,$9,'pending')",ident,'staff:'+key,start,end,end+timedelta(hours=1),json.dumps(data),json.dumps(quote),self.settings.rules_hash,clock)
            row=unpack(await db.fetchrow('SELECT * FROM bookings WHERE id=$1',ident))
            await self.audit(db,ident,actor,'staff_submitted',dict(rules_consent_obtained=False))
            await self.enqueue(db,row,'staff_submitted','Администратор создал бронь по телефону.\n'+card(row),self.settings.admin_ids)
            return ident,True
