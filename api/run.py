"""
Vercel serverless entry point. Wraps tracker.py's run flow (ingest, then
 digest, then send) behind an HTTP GET, triggered daily by the cron
schedule in vercel.json.

Requires POSTGRES_URL (or DATABASE_URL) to be set -- see db.py. Without
one this would fall back to a local SQLite file, which does not persist
across invocations on Vercel and is not a safe way to run this in
production (state -- what's already been digested -- would reset on
every cold start, risking duplicate or dropped digest emails).

Auth: if CRON_SECRET is set, requests must carry
'Authorization: Bearer <CRON_SECRET>' -- Vercel Cron Jobs attach this
header automatically for any project with CRON_SECRET set as an env
var, so cron-triggered calls are authenticated for free. A manual GET
without that header is rejected the same way. Leave CRON_SECRET unset
only for local `vercel dev` testing.
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tracker  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        expected = os.environ.get("CRON_SECRET")
        if expected and self.headers.get("Authorization", "") != f"Bearer {expected}":
            self._json(401, {"status": "error", "error": "unauthorized"})
            return

        if not (os.environ.get("POSTGRES_URL") or os.environ.get("DATABASE_URL")):
            self._json(500, {
                "status": "error",
                "error": "POSTGRES_URL/DATABASE_URL not set -- refusing to run "
                         "against ephemeral local storage in production.",
            })
            return

        try:
            conn = tracker.connect()
            try:
                tracker.ingest(conn, days=1)
                body, names = tracker.build_digest(conn)
                if body:
                    tracker.send(body)
                    conn.executemany(
                        "UPDATE applications SET digested = 1 WHERE name = ?",
                        [(name,) for name in names],
                    )
                    conn.commit()
                    result = {"status": "ok", "leads_digested": len(names)}
                else:
                    result = {
                        "status": "ok",
                        "leads_digested": 0,
                        "note": "nothing new to send",
                    }
            finally:
                conn.close()
        except Exception as exc:
            self._json(500, {"status": "error", "error": str(exc)})
            return

        self._json(200, result)

    def _json(self, code, payload):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())
