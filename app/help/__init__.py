"""Help: a read-only answer service for Ming Hwee office staff (OS Portal).

Its own FastAPI app (app/help/main.py, `uvicorn app.help.main:app`), its own
container (`chatbot-help`), its own settings, model client, concurrency cap and
timeouts. It reuses the bot's knowledge-base search and guards and changes
nothing of the bot's. Every request runs inside app.readonly.read_only().
"""
