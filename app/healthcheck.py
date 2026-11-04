"""The container's healthcheck: `python -m app.healthcheck`. Exit 0 healthy, 1 not.

/health/ready always answers 200, so the verdict is in the body; curl -f cannot
see it, and /health says "ok" whatever state the bot is in. Standard library
only and nothing imported from the app, so it stays cheap at one run per 30s and
cannot fail for a reason of its own. Docker only marks the container unhealthy;
it does not restart it, so a short database failure costs a label and no replies.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

# Under the healthcheck's own 5s timeout, so a slow answer is reported here.
TIMEOUT_SECONDS = 4.0


def ready(body: object) -> bool:
    return isinstance(body, dict) and body.get("status") == "ok"


def main(url: str | None = None) -> int:
    url = url or f"http://localhost:{os.getenv('PORT', '8000')}/health/ready"
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as response:
            body = json.load(response)
    except Exception as exc:  # noqa: BLE001 - any failure to answer is unhealthy
        print(f"not ready: {type(exc).__name__}", file=sys.stderr)
        return 1
    if not ready(body):
        print(f"not ready: {json.dumps(body)}", file=sys.stderr)
    return 0 if ready(body) else 1


if __name__ == "__main__":
    raise SystemExit(main())
