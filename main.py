"""Entrypoint for the Bothost core card: aiohttp on $PORT."""
import asyncio
import logging
import os

from aiohttp import web

from booking_store import BookingStore
from core.app import build_app
from settings import load

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
log = logging.getLogger('core')


async def main():
    settings = load()
    if not settings.database_url:
        raise SystemExit('DATABASE_URL is required')
    token = os.getenv('CORE_API_TOKEN', '').strip()
    if len(token) < 32:
        raise SystemExit('CORE_API_TOKEN must be at least 32 characters')
    store = await BookingStore.connect(settings)
    app = build_app(store, settings, token)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    port = int(os.getenv('PORT', '3000'))
    await web.TCPSite(runner, '0.0.0.0', port).start()
    log.info('pihta-core listening on 0.0.0.0:%s', port)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        await store.pool.close()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as error:  # Bothost hides secrets in tracebacks; log the cause first
        log.error('startup failed: %s: %s', type(error).__name__, str(error)[:300])
        raise
