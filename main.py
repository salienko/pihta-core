"""Entrypoint for the Bothost core card: aiohttp on $PORT, workers in-process."""
import asyncio
import logging
import os

from aiohttp import web

from booking_store import BookingStore, WorkerLock
from core.app import build_app
from core.workers import channels_from_env, cleanup_loop, delivery_loop, payments_loop
from settings import load

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
log = logging.getLogger('core')

WORKER_LOCK = 7319846
# How often the advisory lock is re-checked. The lock is session-scoped: if the
# connection holding it dies, PostgreSQL drops it silently and a second core
# instance may come up -> double notifications. One minute is enough: a lost
# lock costs at most a minute of overlapping work.
LOCK_CHECK_INTERVAL = 60


async def lock_watchdog(lock, on_lost, retries=2, retry_delay=5):
    """Re-check the advisory lock periodically; report the loss if it happens.

    A check that failed to reach the database is not yet a lost lock: retry a
    couple of times before stopping the workers, so a short database outage
    does not take the core down.
    """
    while True:
        await asyncio.sleep(LOCK_CHECK_INTERVAL)
        missing = 0
        while True:
            try:
                held = await lock.holds()
            except Exception as error:
                log.error('lock check failed: %s: %s', type(error).__name__, str(error)[:200])
                held = False
            if held:
                break
            missing += 1
            if missing > retries:
                break
            log.warning('advisory lock not confirmed, re-checking in %ss', retry_delay)
            await asyncio.sleep(retry_delay)
        if held:
            continue
        log.critical('advisory lock lost, another core instance may be running')
        await on_lost()
        return


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

    lock = WorkerLock(store.pool, WORKER_LOCK)
    if not await lock.acquire():
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

    stop = asyncio.Event()
    lost = []

    async def on_lock_lost():
        # Stop the background jobs first: they are the ones that send
        # notifications, and a second instance may already be doing the same.
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        log.critical('background tasks stopped, shutting the core down')
        lost.append(True)
        stop.set()

    watchdog = asyncio.create_task(lock_watchdog(lock, on_lock_lost))
    try:
        await stop.wait()
    finally:
        watchdog.cancel()
        for task in tasks:
            task.cancel()
        await runner.cleanup()
        await lock.release()
        await store.pool.close()
    if lost:
        # Non-zero exit: the platform must bring this instance back, and the
        # new process will either take the lock or refuse to start because
        # another instance already owns it.
        raise SystemExit('advisory lock lost, core stopped')


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as error:  # Bothost hides secrets in tracebacks; log the cause first
        log.error('startup failed: %s: %s', type(error).__name__, str(error)[:300])
        raise
