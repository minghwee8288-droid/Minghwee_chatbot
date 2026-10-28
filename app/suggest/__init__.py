"""Reply drafts for a human consultant: POST /internal/suggest.

Read-only and secret-protected. Called server to server by the Smart Inbox.
It drafts with Claire's own prompt, retrieval and guards, and never sends,
never writes, and never touches the live bot's in-memory state. Registered by
app/main.py only when SUGGEST_SECRET is set.
"""
