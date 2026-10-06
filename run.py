"""
Run the Django version of the voice agent.

Django's own `manage.py runserver` only speaks WSGI, which can't carry the
/ws WebSocket that Twilio's Media Stream needs -- so this runs the ASGI app
(voiceagent.asgi:application) under uvicorn directly, the same way app.py
was served before the Django migration.

Run: python run.py
Then open http://localhost:8000
"""

import uvicorn

if __name__ == "__main__":
    uvicorn.run("voiceagent.asgi:application", host="0.0.0.0", port=8000, reload=True)
