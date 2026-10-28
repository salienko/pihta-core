"""Гость — человек, а не аккаунт.

Телефон — ключ гостя (guests.phone уникален), аккаунты Telegram и MAX
привязываются к нему строками guest_accounts. Склеить аккаунты можно только
по номеру, подтверждённому кнопкой «поделиться контактом»; номер, введённый
руками, гостем не управляет.

Брони остаются в bookings с прежним telegram_id: доступ считается по всем
аккаунтам гостя, а не по одному actor.
"""
from booking_domain import RuleError, clean_name, normalize_phone

PLATFORMS = ('telegram', 'max')


class GuestStore:
    # ---------- чтение ----------

    async def platform_of(self, db, actor, platform=None):
        """Платформа аккаунта: явная, иначе последняя, откуда актор писал."""
        return platform or await db.fetchval(
            'SELECT platform FROM actor_platforms WHERE actor=$1', actor) or 'telegram'

    async def account_of(self, db, actor, platform=None):
        """Строка guest_accounts для актора или None."""
        platform = await self.platform_of(db, actor, platform)
        row = await db.fetchrow('SELECT * FROM guest_accounts WHERE platform=$1 AND external_id=$2',
                                platform, actor)
        if row:
            return row
        # Платформа могла смениться, а привязка осталась за прежней: берём
        # единственную привязку, если она однозначна.
        rows = await db.fetch('SELECT * FROM guest_accounts WHERE external_id=$1', actor)
        return rows[0] if len(rows) == 1 else None

    async def guest_of(self, db, actor, platform=None):
        account = await self.account_of(db, actor, platform)
        return await db.fetchrow('SELECT * FROM guests WHERE id=$1', account['guest_id']) if account else None

    async def guest(self, guest_id, db=None):
        return await (db or self.pool).fetchrow('SELECT * FROM guests WHERE id=$1', int(guest_id))

    async def guest_by_phone(self, phone, db=None):
        """Найти гостя по номеру: телефон уникален и служит ключом гостя."""
        return await (db or self.pool).fetchrow('SELECT * FROM guests WHERE phone=$1',
                                                normalize_phone(phone))

    async def guest_accounts(self, guest_id, db=None):
        """Все аккаунты гостя, свежие первыми."""
        return [dict(r) for r in await (db or self.pool).fetch(
            'SELECT * FROM guest_accounts WHERE guest_id=$1 ORDER BY last_seen_at DESC,platform', int(guest_id))]

    async def guest_of_booking(self, ident, db=None):
        db = db or self.pool
        owner = await db.fetchval('SELECT telegram_id FROM bookings WHERE id=$1', ident)
        return await self.guest_of(db, owner) if owner is not None else None

    async def account_ids(self, db, actor, platform=None):
        """Id аккаунтов гостя: по ним собираются его брони; без гостя — только сам актор."""
        guest = await self.guest_of(db, actor, platform)
        if not guest:
            return [actor], None
        rows = await db.fetch('SELECT external_id FROM guest_accounts WHERE guest_id=$1', guest['id'])
        return sorted({r['external_id'] for r in rows} | {actor}), guest

    async def owns(self, db, actor, owner_id):
        """Бронь принадлежит гостю, если её telegram_id — любой его аккаунт."""
        if owner_id is None:
            return False
        if owner_id == actor:
            return True
        ids, guest = await self.account_ids(db, actor)
        return bool(guest) and owner_id in ids

    async def contact_card(self, db, actor):
        """Имя и телефон для черновика: из гостя, иначе из прежнего профиля."""
        guest = await self.guest_of(db, actor)
        if guest:
            return dict(name=guest['name'], phone=guest['phone'])
        row = await db.fetchrow('SELECT * FROM profiles WHERE telegram_id=$1', actor)
        return dict(name=row['name'], phone=row['phone']) if row else None

    async def guest_list(self, limit=200):
        """Гости для админки: карточка + аккаунты."""
        guests = [dict(r) for r in await self.pool.fetch(
            'SELECT * FROM guests ORDER BY updated_at DESC,id DESC LIMIT $1', limit)]
        accounts = {}
        for row in await self.pool.fetch('SELECT * FROM guest_accounts ORDER BY last_seen_at DESC'):
            accounts.setdefault(row['guest_id'], []).append(dict(row))
        for guest in guests:
            guest['accounts'] = accounts.get(guest['id'], [])
        return guests

    # ---------- активность и доставка ----------

    async def touch(self, actor, platform):
        """Любое действие актора: запоминаем платформу и время для доставки."""
        async with self.pool.acquire() as db:
            await db.execute(
                'INSERT INTO actor_platforms(actor,platform,updated_at) VALUES($1,$2,now()) '
                'ON CONFLICT(actor) DO UPDATE SET platform=EXCLUDED.platform,updated_at=now()',
                actor, platform)
            await db.execute('UPDATE guest_accounts SET last_seen_at=now() '
                             'WHERE platform=$1 AND external_id=$2', platform, actor)

    async def delivery_target(self, recipient, allowed=None):
        """Канал доставки: аккаунт гостя с последней активностью.

        allowed — платформы с настроенным каналом: если последний мессенджер
        гостя ещё не подключён, берём следующий его аккаунт, чтобы сообщение
        не потерялось.
        """
        async with self.pool.acquire() as db:
            account = await self.account_of(db, recipient)
            if not account:
                return dict(platform=await self.platform_of(db, recipient), external_id=recipient)
            rows = [dict(r) for r in await db.fetch(
                'SELECT * FROM guest_accounts WHERE guest_id=$1 ORDER BY last_seen_at DESC,platform',
                account['guest_id'])]
            if allowed:
                rows = [row for row in rows if row['platform'] in allowed] or rows
            newest = rows[0]
            return dict(platform=newest['platform'], external_id=newest['external_id'])

    # ---------- подтверждённый контакт ----------

    async def confirm_contact(self, actor, phone, name='', platform=None):
        """Номер из кнопки «поделиться контактом».

        result='bound'    — аккаунт привязан к гостю с этим номером;
        result='conflict' — номер уже за другим гостем, нужен ответ «это вы?».
        """
        phone = normalize_phone(phone)
        name = clean_name(name) if name and name.strip() else ''
        async with self.tx() as db:
            platform = await self.platform_of(db, actor, platform)
            account = await db.fetchrow('SELECT * FROM guest_accounts WHERE platform=$1 AND external_id=$2',
                                        platform, actor)
            current = await db.fetchrow('SELECT * FROM guests WHERE id=$1', account['guest_id']) if account else None
            owner = await self.guest_by_phone(phone, db)
            if owner and current and owner['id'] == current['id']:
                return dict(result='bound', guest=dict(owner), platform=platform)
            if owner:
                # Чужой номер: склеиваем только после подтверждения «это я».
                return dict(result='conflict', guest=dict(owner), platform=platform,
                            current=dict(current) if current else None)
            if current:
                # Номер свободен: обновляем запись гостя, а не создаём нового.
                guest = await db.fetchrow('UPDATE guests SET phone=$2,'
                                          "name=CASE WHEN $3='' THEN name ELSE $3 END,"
                                          'updated_at=now() WHERE id=$1 RETURNING *', current['id'], phone, name)
            else:
                guest = await db.fetchrow('INSERT INTO guests(phone,name) VALUES($1,$2) RETURNING *',
                                          phone, name)
            await self.bind(db, actor, platform, guest['id'])
            return dict(result='bound', guest=dict(guest), platform=platform)

    async def bind(self, db, external_id, platform, guest_id):
        await db.execute(
            'INSERT INTO guest_accounts(platform,external_id,guest_id,verified) VALUES($1,$2,$3,true) '
            'ON CONFLICT(platform,external_id) DO UPDATE SET guest_id=EXCLUDED.guest_id,'
            'verified=true,last_seen_at=now()', platform, external_id, guest_id)

    async def merge_account_into(self, actor, guest_id, name='', platform=None):
        """«Да, это я»: аккаунт уходит к гостю с занятым номером, прежний гость вливается."""
        async with self.tx() as db:
            platform = await self.platform_of(db, actor, platform)
            target = await db.fetchrow('SELECT * FROM guests WHERE id=$1', int(guest_id))
            if not target:
                raise RuleError('Данные гостя изменились. Подтвердите номер ещё раз.')
            account = await db.fetchrow('SELECT * FROM guest_accounts WHERE platform=$1 AND external_id=$2',
                                        platform, actor)
            if account and account['guest_id'] != target['id']:
                await self.merge_guests(db, account['guest_id'], target['id'])
            await self.bind(db, actor, platform, target['id'])
            name = clean_name(name) if name and name.strip() else ''
            if name:
                await db.execute("UPDATE guests SET name=$2,updated_at=now() WHERE id=$1 AND name=''",
                                 target['id'], name)
            return dict(guest=dict(target), platform=platform)

    async def merge_guests(self, db, source_id, target_id):
        """Аккаунты переносим, брони не трогаем: доступ считается по аккаунтам."""
        if not source_id or source_id == target_id:
            return
        await db.execute('UPDATE guest_accounts SET guest_id=$1 WHERE guest_id=$2', target_id, source_id)
        await db.execute('DELETE FROM guests WHERE id=$1', source_id)

    async def save_profile(self, actor, name, phone, confirmed=False, platform=None):
        """«Мои данные»: профиль актора и номер гостя.

        Подтверждённый номер связывает аккаунты, введённый руками — только
        меняет номер своей записи гостя и никогда никого не склеивает.
        """
        name, phone = clean_name(name), normalize_phone(phone)
        if confirmed:
            result = await self.confirm_contact(actor, phone, name, platform)
            if result['result'] == 'bound':
                await self.profile(actor, name, phone)
            return result
        async with self.tx() as db:
            guest = await self.guest_of(db, actor, platform)
            if guest and guest['phone'] != phone and await db.fetchval(
                    'SELECT 1 FROM guests WHERE phone=$1 AND id<>$2', phone, guest['id']):
                # Номер занят: введённый руками номер никого не склеивает.
                return dict(result='taken', guest=dict(guest))
            await db.execute('INSERT INTO profiles VALUES($1,$2,$3) ON CONFLICT(telegram_id) '
                             'DO UPDATE SET name=$2,phone=$3', actor, name, phone)
            if not guest:
                return dict(result='stored')
            guest = await db.fetchrow('UPDATE guests SET phone=$2,name=$3,updated_at=now() '
                                      'WHERE id=$1 RETURNING *', guest['id'], phone, name)
            return dict(result='bound', guest=dict(guest))

    # ---------- ручное управление из админки ----------

    async def attach_account(self, actor, guest_id, platform, external_id):
        self.admin(actor)
        guest_id, external_id = self._guest_id(guest_id), self._account_id(external_id)
        if platform not in PLATFORMS:
            raise RuleError('Выберите мессенджер: Telegram или MAX.')
        async with self.tx() as db:
            guest = await db.fetchrow('SELECT * FROM guests WHERE id=$1', guest_id)
            if not guest:
                raise RuleError('Гость не найден.')
            other = await db.fetchval('SELECT guest_id FROM guest_accounts WHERE platform=$1 AND external_id=$2',
                                      platform, external_id)
            if other and other != guest_id:
                raise RuleError('Аккаунт уже привязан к другому гостю. Сначала отвяжите его.')
            await self.bind(db, external_id, platform, guest_id)
            await self.audit(db, None, actor, 'guest_attach',
                             dict(guest=guest_id, platform=platform, external_id=external_id))
            return dict(guest)

    async def detach_account(self, actor, guest_id, platform, external_id):
        self.admin(actor)
        guest_id, external_id = self._guest_id(guest_id), self._account_id(external_id)
        if platform not in PLATFORMS:
            raise RuleError('Выберите мессенджер: Telegram или MAX.')
        async with self.tx() as db:
            result = await db.execute('DELETE FROM guest_accounts WHERE platform=$1 AND external_id=$2 AND guest_id=$3',
                                      platform, external_id, guest_id)
            if result != 'DELETE 1':
                raise RuleError('Этот аккаунт не привязан к гостю. Обновите страницу.')
            await self.audit(db, None, actor, 'guest_detach',
                             dict(guest=guest_id, platform=platform, external_id=external_id))

    @staticmethod
    def _guest_id(value):
        try:
            guest_id = int(value)
        except (TypeError, ValueError):
            raise RuleError('Проверьте номер гостя.') from None
        if guest_id <= 0:
            raise RuleError('Проверьте номер гостя.')
        return guest_id

    @staticmethod
    def _account_id(value):
        value = str(value).strip()
        if not value.isdigit() or not 0 < int(value) < 10 ** 18:
            raise RuleError('ID аккаунта — это число, которое бот показывает в /id.')
        return int(value)
