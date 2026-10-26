"""PostgreSQL order journal and transactional outbox. No reservation or payment state."""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
import json
from pathlib import Path
import re
import uuid

import asyncpg

from domain import RuleError, clean_name, normalize_phone, now, order_card, quote, visit_end

SCHEMA = 'pihta_intake_v1'


class Store:
    def __init__(self, settings, pool):
        self.settings, self.pool = settings, pool

    @classmethod
    async def connect(cls, settings, schema=SCHEMA):
        if not re.fullmatch(r'[a-z][a-z0-9_]{0,62}', schema):
            raise ValueError('Invalid schema name')
        if not settings.database_url.startswith(('postgres://', 'postgresql://')):
            raise ValueError('Задайте DATABASE_URL для PostgreSQL.')
        pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=5,
                                        command_timeout=20, server_settings={'search_path': schema})
        try:
            async with pool.acquire() as db:
                async with db.transaction():
                    await db.execute('SELECT pg_advisory_xact_lock(7319843)')
                    await db.execute(f'CREATE SCHEMA IF NOT EXISTS {schema}')
                    await db.execute(Path(__file__).with_name('schema.sql').read_text(encoding='utf-8'))
        except BaseException:
            await pool.close()
            raise
        return cls(settings, pool)

    @asynccontextmanager
    async def transaction(self):
        async with self.pool.acquire() as db:
            async with db.transaction():
                yield db

    @staticmethod
    def unpack(row):
        if not row:
            raise RuleError('Начните оформление через «Оставить заказ».')
        return {'token': row['token'], 'state': row['state'],
                'data': json.loads(row['data']), 'revision': row['revision']}

    async def draft(self, actor, token=None):
        d = self.unpack(await self.pool.fetchrow('SELECT * FROM drafts WHERE telegram_id=$1', actor))
        if token and d['token'] != token:
            raise RuleError('Эта кнопка из старого заказа. Откройте текущее оформление.')
        return d

    async def begin(self, actor):
        token = uuid.uuid4().hex[:12]
        await self.pool.execute('''INSERT INTO drafts(telegram_id,token,state,data,updated_at) VALUES($1,$2,'date','{}',now())
            ON CONFLICT(telegram_id) DO UPDATE SET token=excluded.token,state='date',data='{}',
            updated_at=now(),revision=drafts.revision+1''', actor, token)
        return token

    async def save(self, actor, draft, state, data):
        result = await self.pool.execute('''UPDATE drafts SET state=$1,data=$2::jsonb,revision=revision+1,updated_at=now()
            WHERE telegram_id=$3 AND token=$4 AND revision=$5''', state,
            json.dumps(data, ensure_ascii=False), actor, draft['token'], draft['revision'])
        if result != 'UPDATE 1':
            raise RuleError('Оформление уже изменилось. Откройте текущий заказ.')

    async def adjust_service(self, actor, token, code, delta):
        catalog = {item['code']: item for item in self.settings.content['services']}
        if code not in catalog or delta not in (-1, 1):
            raise RuleError('Услуга недоступна.')
        async with self.transaction() as db:
            d = self.unpack(await db.fetchrow('SELECT * FROM drafts WHERE telegram_id=$1 FOR UPDATE', actor))
            if d['token'] != token or d['state'] != 'services':
                raise RuleError('Откройте услуги в текущем заказе.')
            data = d['data']
            cart = data.setdefault('cart', {})
            quantity = cart.get(code, 0) + delta
            if not 0 <= quantity <= 100:
                raise RuleError('Количество от 0 до 100.')
            if quantity:
                cart[code] = quantity
            else:
                cart.pop(code, None)
            data['consent'] = False
            await db.execute('UPDATE drafts SET data=$1::jsonb,revision=revision+1,updated_at=now() WHERE telegram_id=$2',
                             json.dumps(data, ensure_ascii=False), actor)

    async def submit(self, actor, token, username='', clock=None):
        async with self.transaction() as db:
            # Serialize submissions and draft replacement for this guest, including duplicate clicks.
            row = await db.fetchrow('SELECT * FROM drafts WHERE telegram_id=$1 FOR UPDATE', actor)
            old = await db.fetchval('SELECT id FROM orders WHERE draft_token=$1 AND telegram_id=$2', token, actor)
            if old:
                return old, False
            d = self.unpack(row)
            if d['token'] != token or d['state'] != 'review':
                raise RuleError('Откройте итоговую карточку текущего заказа.')
            clock = clock or await db.fetchval('SELECT clock_timestamp()')
            data = d['data']
            visit_end(datetime.fromisoformat(data['start']), data['hours'], clock)
            data['name'] = clean_name(data['name'])
            data['phone'] = normalize_phone(data['phone'])
            catalog = {item['code']: item for item in self.settings.content['services']}
            estimate = quote(data, catalog, self.settings.content.get('holidays', {}))
            if estimate != data.get('quote'):
                raise RuleError('Расчёт изменился. Откройте итог заново и проверьте стоимость.')
            if not data.get('consent') or data.get('rules_hash') != self.settings.rules_hash:
                raise RuleError('Ознакомьтесь с актуальными правилами и подтвердите согласие.')
            if not self.settings.admin_ids:
                raise RuleError('Приём заказов пока настраивается. Свяжитесь с администратором по телефону.')
            data.update(telegram_id=actor, username=username or '')
            ident = uuid.uuid4().hex[:12].upper()
            await db.execute('''INSERT INTO customers VALUES($1,$2,$3,$4,$5) ON CONFLICT(telegram_id) DO UPDATE SET
                username=excluded.username,name=excluded.name,phone=excluded.phone,updated_at=excluded.updated_at''',
                actor, data['username'], data['name'], data['phone'], clock)
            await db.execute('INSERT INTO orders VALUES($1,$2,$3,$4::jsonb,$5::jsonb,$6,$7,$7)',
                             ident, token, actor, json.dumps(data, ensure_ascii=False), json.dumps(estimate, ensure_ascii=False),
                             self.settings.rules_hash, clock)
            text = order_card(ident, data, estimate, admin=True)
            for admin in self.settings.admin_ids:
                await db.execute('INSERT INTO deliveries(order_id,admin_id,text) VALUES($1,$2,$3)', ident, admin, text)
            await db.execute("UPDATE drafts SET state='submitted',revision=revision+1,updated_at=now() WHERE telegram_id=$1", actor)
            return ident, True

    async def orders(self, actor, admin=False, offset=0):
        if admin and actor not in self.settings.admin_ids:
            raise RuleError('Нет доступа.')
        rows = await self.pool.fetch('''SELECT * FROM orders WHERE ($1::boolean OR telegram_id=$2)
            ORDER BY created_at DESC,id DESC LIMIT 11 OFFSET $3''', admin, actor, max(0, offset))
        return [dict(row) for row in rows[:10]], len(rows) > 10

    async def get_order(self, actor, ident):
        row = await self.pool.fetchrow('''SELECT o.*, (SELECT count(*) FROM deliveries d
            WHERE d.order_id=o.id AND d.delivered_at IS NOT NULL) AS delivery_count
            FROM orders o WHERE id=$1''', ident)
        if not row or (row['telegram_id'] != actor and actor not in self.settings.admin_ids):
            raise RuleError('Заказ не найден или недоступен.')
        return dict(row)

    async def next_delivery(self, clock=None):
        async with self.transaction() as db:
            clock = clock or await db.fetchval('SELECT clock_timestamp()')
            row = await db.fetchrow('''SELECT * FROM deliveries WHERE delivered_at IS NULL
                AND due_at<=$1 AND (locked_until IS NULL OR locked_until<=$1) AND admin_id=ANY($2::bigint[])
                ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1''', clock, list(self.settings.admin_ids))
            if not row:
                return None
            await db.execute('UPDATE deliveries SET locked_until=$1,attempts=attempts+1 WHERE id=$2', clock + timedelta(minutes=2), row['id'])
            return dict(row)

    async def delivered(self, ident):
        await self.pool.execute('UPDATE deliveries SET delivered_at=now(),locked_until=NULL,last_error=NULL WHERE id=$1', ident)

    async def failed(self, ident, code, retry_after=None):
        attempts = await self.pool.fetchval('SELECT attempts FROM deliveries WHERE id=$1', ident)
        delay = max(5, retry_after) if retry_after is not None else min(3600, 5 * 2 ** min(attempts, 10))
        await self.pool.execute("UPDATE deliveries SET due_at=now()+$1*interval '1 second',locked_until=NULL,last_error=$2 WHERE id=$3",
                                float(delay), code[:80], ident)

    async def delivery_status(self, actor):
        if actor not in self.settings.admin_ids:
            raise RuleError('Нет доступа.')
        return dict(await self.pool.fetchrow('''SELECT count(*) AS pending,
            count(*) FILTER(WHERE last_error IS NOT NULL) AS errors
            FROM deliveries WHERE delivered_at IS NULL'''))
