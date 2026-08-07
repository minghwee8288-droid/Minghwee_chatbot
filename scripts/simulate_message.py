"""Send a fake Whapi webhook to a locally running chatbot.

    python scripts/simulate_message.py "how much for a filipino helper" --phone 6591234567

Use this to exercise the pipeline before pointing a real WhatsApp number at it.
Add --from-me to simulate a human agent replying from the WA Business app,
which should silence the bot on that conversation.
"""

from __future__ import annotations

import argparse
import os
import time
import uuid

import httpx


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("message", help="Message body to send")
    parser.add_argument("--phone", default="6591234567", help="Client phone, digits only")
    parser.add_argument("--name", default="Test Client", help="WhatsApp display name")
    parser.add_argument("--url", default="http://localhost:8000/webhook/whapi")
    parser.add_argument(
        "--from-me",
        action="store_true",
        help="Simulate an outbound message typed by a human agent",
    )
    args = parser.parse_args()

    payload = {
        "messages": [
            {
                "id": f"wamid.SIM{uuid.uuid4().hex[:12].upper()}",
                "from_me": args.from_me,
                "type": "text",
                "chat_id": f"{args.phone}@s.whatsapp.net",
                "from": args.phone,
                "from_name": args.name,
                "timestamp": int(time.time()),
                "text": {"body": args.message},
            }
        ],
        "event": {"type": "messages", "event": "post"},
    }

    headers = {}
    secret = os.getenv("WHAPI_WEBHOOK_SECRET")
    if secret:
        headers["X-Whapi-Secret"] = secret

    response = httpx.post(args.url, json=payload, headers=headers, timeout=30)
    print(f"{response.status_code} {response.text}")


if __name__ == "__main__":
    main()
