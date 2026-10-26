"""JSON API of the booking core. Transports call it over HTTPS with a shared token."""
import hmac
import json
import logging

from aiohttp import web

from booking_domain import RuleError
from core import __version__
from core.screens import Screens

log = logging.getLogger('core.api')


def _json(data, status=200):
    return web.json_response(data, status=status, dumps=lambda o: json.dumps(o, ensure_ascii=False))


@web.middleware
async def api_auth(request, handler):
    token = request.app.get('core_token', '')
    supplied = request.headers.get('X-Core-Token', '')
    if not token or not hmac.compare_digest(supplied, token):
        return _json({"error": "unauthorized"}, 401)
    try:
        return await handler(request)
    except RuleError as error:
        return _json({"error": str(error)}, 400)
    except Exception:
        log.exception('api failure %s %s', request.method, request.path)
        return _json({"error": "internal"}, 500)


async def ping(request):
    return _json({"ok": True, "version": __version__, "release": "core-0.1"})


async def screen(request):
    try:
        payload = await request.json()
    except Exception:
        return _json({"error": "bad json"}, 400)
    try:
        actor = int(payload.get('user_id'))
    except (TypeError, ValueError):
        return _json({"error": "user_id required"}, 400)
    platform = str(payload.get('platform') or '')
    if platform not in ('telegram', 'max'):
        return _json({"error": "platform must be telegram or max"}, 400)
    result = await request.app['screens'].dispatch(actor, payload.get('action') or 'home')
    body = result.json()
    body.update(platform=platform, actor=actor)
    return _json(body)


def add_routes(app):
    """Mount the JSON API as a subapp; a subapp has its own state, so seed it."""
    api = web.Application(middlewares=[api_auth], client_max_size=65536)
    api['core_token'] = app['core_token']
    api['screens'] = Screens(app['settings'], app['store'])
    api.add_routes([web.get('/ping', ping), web.post('/screen', screen)])
    app.add_subapp('/api/v1/', api)
