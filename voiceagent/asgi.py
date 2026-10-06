"""
ASGI config for voiceagent project.

Django itself only serves plain HTTP here -- there's no Channels consumer.
The Twilio Media Stream WebSocket at /ws is handled by a tiny raw-ASGI
dispatcher instead: pipecat's FastAPIWebsocketTransport only needs an object
shaped like Starlette's WebSocket (receive()/send_bytes()/send_text()/
close()/application_state/client_state) -- which is exactly what
starlette.websockets.WebSocket is, independent of FastAPI or Django. Routing
/ws to it directly avoids needing Channels (whose consumer interface doesn't
match what pipecat expects) just to keep that one endpoint working.
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'voiceagent.settings')

django_application = get_asgi_application()

# Imported after Django's own ASGI app is set up, since agent.ws_server
# imports main.py, which expects DJANGO_SETTINGS_MODULE/env already resolved.
from agent.ws_server import websocket_application  # noqa: E402


async def application(scope, receive, send):
    if scope["type"] == "websocket" and scope["path"] == "/ws":
        await websocket_application(scope, receive, send)
    else:
        await django_application(scope, receive, send)
