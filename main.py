"""Entrypoint for the Bothost core card: aiohttp on $PORT, workers in-process."""
import asyncio
import logging
import os

from aiohttp import web

from booking_store import BookingStore
from core.app import build_app
from core.workers import channels_from_env, cleanup_loop, delivery_loop, payments_loop
from settings import load

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
log = logging.getLogger('core')

WORKER_LOCK = 7319846


async def main():
    settings = load()
    if not settings.database_url:
        raise SystemExit('DATABASE_URL is required')
    token = os.getenv('CORE_API_TOKEN', '').strip()
    if len(token) < 32:
        raise SystemExit('CORE_API_TOKEN must be at least 32 characters')
    store = await BookingStore.connect(settings)
    await asyncio.gather(*[store.pool.fetchval('SELECT 1') for _ in range(5)])
    log.info('database pool warmed')

    lock = await store.pool.acquire()
    if not await lock.fetchval('SELECT pg_try_advisory_lock($1)', WORKER_LOCK):
        raise SystemExit('Another core instance is running')

    channels = channels_from_env(settings)
    tasks = [asyncio.create_task(delivery_loop(store, settings, channels))] if channels else []
    tasks.append(asyncio.create_task(cleanup_loop(store)))
    if settings.paykeeper_enabled:
        tasks.append(asyncio.create_task(payments_loop(store)))

    app = build_app(store, settings, token)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    port = int(os.getenv('PORT', '3000'))
    await web.TCPSite(runner, '0.0.0.0', port).start()
    log.info('release=%s paykeeper=%s channels=%s',
             os.getenv('RELEASE', 'dev'), settings.paykeeper_enabled,
             ','.join(sorted(channels)) or 'none')
    log.info('pihta-core listening on 0.0.0.0:%s', port)
    try:
        await asyncio.Event().wait()
    finally:
        for task in tasks:
            task.cancel()
        await runner.cleanup()
        await store.pool.close()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as error:  # Bothost hides secrets in tracebacks; log the cause first
        log.error('startup failed: %s: %s', type(error).__name__, str(error)[:300])
        raise
