"""Background jobs of the core: payment provider calls and notification delivery.

Nothing here knows about Telegram or MAX APIs: notifications are posted to the
channel's /deliver endpoint, and the channel renders them with its own means.
"""
import asyncio
import logging

from aiohttp import ClientSession, ClientTimeout

from booking_domain import rub
from core.screens import btn

log = logging.getLogger('core.workers')

DELIVER_TIMEOUT = ClientTimeout(total=10)


def buttons_for(row, settings, payment=None, receipt_url=None):
    """Build notification buttons; same rules as the original bot had."""
    admin = row['recipient'] in settings.admin_ids
    rows = [[btn('Открыть веб-панель', 'staffweb')]] if admin else [[btn('Открыть бронь', 'open:' + row['booking_id'])]]
    if payment:
        if row['event'].startswith('paylink:') and payment.get('url'):
            automatic = payment.get('automatic')
            check = ('paycheck:' if automatic else 'payreport:') + payment['id']
            label = ('Проверить оплату ' + rub(payment['amount'])) if automatic else '✅ Уже оплатил'
            rows = [[btn('🔗 Перейти к оплате', url=payment['url'])], [btn(label, check)],
                    [btn('Открыть бронь', 'open:' + row['booking_id'])]]
        elif admin:
            rows = [[btn('✅ Получено ' + rub(payment['amount']), 'paystaff:yes:' + payment['id'])],
                    [btn('❌ Деньги не поступили', 'paystaff:no:' + payment['id'])]] + rows
    if receipt_url:
        rows = [[btn('Открыть чек', url=receipt_url)]] + rows
    return rows


async def deliver_once(store, settings, channels, session):
    row = await store.next_delivery()
    if not row:
        return False
    try:
        owner = await store.pool.fetchval('SELECT telegram_id FROM bookings WHERE id=$1', row['booking_id'])
        if row['recipient'] not in settings.admin_ids and not await store.owns(store.pool, row['recipient'], owner):
            await store.failed(row['id'], 'recipient_access_removed', permanent=True)
            return True
        if row['event'].startswith('reminder:'):
            current = await store.pool.fetchval('SELECT status FROM bookings WHERE id=$1', row['booking_id'])
            expected = 'pending' if ':pending:' in row['event'] else 'confirmed'
            if current != expected:
                await store.delivered(row['id'])
                return True
            if not await store.reserve_delivery_allowed(row['booking_id'], row['event'], row['recipient']):
                await store.delivered(row['id'])
                return True
        if row['event'].startswith(('guestmsg:', 'note:')):
            # Переписка с администратором не зависит от статуса брони: ответить
            # гостю нужно и по отменённой, и по завершённой — но только самому гостю.
            if row['recipient'] not in settings.admin_ids and not await store.owns(
                    store.pool, row['recipient'], owner):
                await store.failed(row['id'], 'recipient_access_removed', permanent=True)
                return True
        payment = await store.payment_delivery(row['event'])
        if row['event'].startswith(('payreq:', 'paylink:', 'payreported:')) and not payment:
            await store.delivered(row['id'])
            return True
        if payment and row['event'].startswith('paylink:'):
            payment = dict(payment)
            payment['automatic'] = bool(await store.pool.fetchval(
                'SELECT 1 FROM paykeeper_invoices WHERE request_id=$1', payment['id']))
        receipt_url = None
        if row['event'].startswith(('pkreceipt:', 'pkreceiptadmin:', 'pkfinal:', 'pkfinalstaff:')) or \
                (row['event'].startswith('pkpaid:') and 'доступен по кнопке' in row['text']):
            receipt_url = await store.receipt_delivery_url(row['event'])
        # Гость мог ответить уже из другого мессенджера: сообщение уходит в его
        # последний активный канал. Уведомления админам идут по их аккаунту.
        where = (dict(platform=await store.platform_of(store.pool, row['recipient']),
                      external_id=row['recipient'])
                 if row['recipient'] in settings.admin_ids
                 else await store.delivery_target(row['recipient'], set(channels)))
        platform = where['platform']
        target = channels.get(platform)
        if not target:
            await store.failed(row['id'], 'channel_missing', permanent=True)
            return True
        payload = {'external_id': where['external_id'], 'text': row['text'], 'dedupe_key': row['event'],
                   'rows': buttons_for(row, settings, payment, receipt_url)}
        async with session.post(target['url'].rstrip('/') + '/deliver', json=payload,
                                headers={'X-Core-Token': target['token']}) as response:
            if response.status == 429:
                retry = 30
                try:
                    retry = int((await response.json()).get('retry_after') or 30)
                except Exception:
                    pass
                await store.failed(row['id'], 'rate_limit', retry)
                return True
            if response.status == 410:
                # Channel says the recipient can never be reached (blocked the bot,
                # unknown chat). Retrying forever only floods the log.
                await store.failed(row['id'], 'recipient_unreachable', permanent=True)
                return True
            if response.status >= 400:
                await store.failed(row['id'], f'channel_{response.status}',
                                   min(3600, 5 * 2 ** min(row['attempts'], 9)))
                return True
        await store.payment_message_sent(row['event'], row['booking_id'], row['recipient'])
        if payment and not row['event'].startswith('paylink:') and row['recipient'] in settings.admin_ids:
            message_id = None
            await store.payment_message_delivered(payment['id'], row['recipient'], message_id)
    except Exception as error:
        log.exception('delivery failed: %s', type(error).__name__)
        await store.failed(row['id'], type(error).__name__,
                           min(3600, 5 * 2 ** min(row['attempts'], 9)))
        return True
    await store.delivered(row['id'])
    return True


async def delivery_loop(store, settings, channels):
    async with ClientSession(timeout=DELIVER_TIMEOUT) as session:
        while True:
            try:
                await store.tick()
                for _ in range(20):
                    if not await deliver_once(store, settings, channels, session):
                        break
            except Exception as error:
                log.error('delivery loop failed: %s', type(error).__name__)
            await asyncio.sleep(10)


CLEANUP_INTERVAL = 24 * 3600


async def cleanup_loop(store, interval=CLEANUP_INTERVAL):
    """Раз в сутки чистим старые данные: черновики, доставленный outbox,
    старый audit, истёкшие pending_inputs. Брони и платежи не трогаем."""
    while True:
        try:
            counts = await store.cleanup_old_data()
            log.info('cleanup done: %s', counts)
        except Exception as error:
            log.error('cleanup failed: %s: %s', type(error).__name__, str(error)[:200])
        await asyncio.sleep(interval)


async def payments_loop(store):
    """PayKeeper: create invoices, poll statuses, receipts, reservations."""
    while True:
        try:
            await store.invoice_once()
            await store.reserve_once()
            await store.settlement_once()
            for _ in range(2):
                if not await store.poll_once():
                    break
            for _ in range(2):
                if not await store.receipt_once():
                    break
            for _ in range(2):
                if not await store.unmatched_receipt_once():
                    break
        except Exception as error:
            log.error('payment loop failed: %s: %s', type(error).__name__, str(error)[:200])
        # Every transaction in this store takes a global advisory lock, so a
        # chatty payment loop starves guest requests. 15s is plenty for a bathhouse.
        await asyncio.sleep(15)


def channels_from_env(settings):
    """Channel endpoints the core may push notifications to."""
    import os
    token = os.getenv('CORE_API_TOKEN', '').strip()
    found = {}
    for platform, key in (('telegram', 'CHANNEL_TELEGRAM_URL'), ('max', 'CHANNEL_MAX_URL')):
        url = os.getenv(key, '').strip()
        if url and token:
            found[platform] = {'url': url, 'token': token}
    return found
