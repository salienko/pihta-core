"""Assemble the core service: admin panel, PayKeeper webhook, health and JSON API."""
from aiohttp import web

from admin_web import make_app
from core.api import add_routes


def build_app(store, settings, token=''):
    app = make_app(store, settings)
    app['store'], app['settings'], app['core_token'] = store, settings, token
    add_routes(app)

    async def status(request):
        db = True
        try:
            await store.pool.fetchval('SELECT 1')
        except Exception:
            db = False
        return web.json_response({"service": "pihta-core", "db": db,
                                  "paykeeper": settings.paykeeper_enabled},
                                 status=200 if db else 503)

    app.add_routes([web.get('/api/status', status)])
    return app
