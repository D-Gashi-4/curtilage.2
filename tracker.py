#!/usr/bin/env python3
"""
Credco planning application tracker.

Daily delta pull from PlanIt (planit.org.uk), scored for commercial finance
relevance, stored in SQLite (local) or Postgres (Vercel), rendered as an
HTML email digest.

Usage:
    python tracker.py ingest            # pull yesterday's changes, score, store
    python tracker.py digest            # render digest of new qualified leads
    python tracker.py run               # ingest then digest then send
    python tracker.py ingest --days 7   # backfill

Environment:
    PLANIT_UA        User-Agent string (PlanIt 403s on invalid UA). Required.
    RESEND_API_KEY   For sending. Omit to write digest.html to disk instead.
    DIGEST_TO        Comma-separated recipients.
    DIGEST_FROM      Sender address on a verified domain.
    POSTGRES_URL /
    DATABASE_URL     Set on Vercel to use Postgres instead of local SQLite.
                      See db.py.
"""

import argparse
import datetime as dt
import html
import json
import os
import sys
import tempfile
import time
import urllib.parse
import urllib.request

import db
import prime

SUPER_PRIME, PRIME = prime.load()

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

API = "https://www.planit.org.uk/api/applics/json"

# Region / county level names. PlanIt includes sub-areas by default, so these
# six cover ~100 LPAs. Verify each against /api/areas/json before first run.
AUTHORITIES = [
    "London",
    "Essex",
    "Hertfordshire",
    "Kent",
    "Surrey",
    "Buckinghamshire",
]

PAGE_SIZE = 300          # PlanIt default; hard caps are 5000 records / 1000kB
REQUEST_PAUSE = 2.0      # seconds between calls. Do not lower without asking PlanIt.
MAX_PAGES = 40           # safety stop per authority

# Application types worth looking at. PlanIt normalises these into app_type.
WANTED_TYPES = {
    "Full", "Outline", "Reserved Matters", "Conditions",
    "Amendment", "Heritage", "Other",
}

# Description patterns that indicate a fundable scheme.
POSITIVE_PATTERNS = [
    ("change of use", 25),
    ("prior approval", 20),
    ("conversion", 20),
    ("demolition of", 15),
    ("erection of", 15),
    ("construction of", 15),
    ("redevelopment", 25),
    ("new build", 20),
    ("dwellings", 25),
    ("dwellinghouses", 25),
    ("residential units", 30),
    ("apartments", 25),
    ("flats", 20),
    ("class q", 25),
    ("class ma", 25),
    ("mixed use", 20),
    ("student accommodation", 20),
    ("care home", 15),
    ("hmo", 15),
    ("houses in multiple occupation", 15),
]

# Householder noise. Any hit here and the record is dropped outright.
KILL_PATTERNS = [
    "lawful development certificate",
    "certificate of lawfulness",
    "loft conversion",
    "rear extension",
    "side extension",
    "front extension",
    "porch",
    "conservatory",
    "outbuilding",
    "garden room",
    "summerhouse",
    "dormer",
    "rooflight",
    "roof light",
    "velux",
    "fenestration",
    "replacement windows",
    "replacement door",
    "boundary wall",
    "fence",
    "decking",
    "hard standing",
    "hardstanding",
    "dropped kerb",
    "vehicular crossover",
    "tree",
    "t1 ",
    "tpo",
    "advertisement consent",
    "signage",
    "shopfront",
    "satellite dish",
    "air conditioning",
    "air source heat pump",
    "solar panel",
    "listed building consent for internal",
    "discharge of condition",
    "non-material amendment",
    "prior notification of demolition",
]

# Corporate suffixes: applicant is an entity, not a householder.
CORPORATE_HINTS = [
    " ltd", " limited", " llp", " plc", " llc",
    "developments", "homes", "properties", "estates",
    "holdings", "investments", "capital", "group",
    "construction", "regeneration", "partnership",
]

# Private client lane. These indicate substantial work to a high-value home,
# not a householder extension. Only scored inside prime postcodes.
PRIVATE_CLIENT_PATTERNS = [
    ("basement", 35),
    ("subterranean", 35),
    ("excavation", 25),
    ("lower ground floor", 20),
    ("swimming pool", 30),
    ("pool house", 30),
    ("replacement dwelling", 40),
    ("demolition of existing dwelling", 40),
    ("demolition of the existing dwelling", 40),
    ("erection of a replacement", 35),
    ("new dwelling in place of", 35),
    ("listed building consent", 15),
    ("comprehensive refurbishment", 30),
    ("substantial demolition", 25),
    ("staff accommodation", 25),
    ("coach house", 20),
    ("gatehouse", 20),
    ("annexe", 15),
    ("tennis court", 20),
    ("stables", 15),
    ("equestrian", 15),
    ("underground garage", 25),
    ("garaging", 15),
    ("mews", 10),
    ("orangery", 10),
]

# Trivial even in a prime postcode. Kills the private client lane.
PRIVATE_CLIENT_KILL = [
    "certificate of lawfulness",
    "lawful development certificate",
    "tree", "tpo", "t1 ", "t2 ",
    "advertisement", "signage", "shopfront",
    "satellite dish", "air conditioning", "air source heat pump",
    "solar panel", "rooflight", "roof light", "velux",
    "boundary wall", "fence", "gate piers", "railings",
    "non-material amendment", "discharge of condition",
    "replacement windows", "replacement door", "fenestration",
    "hard standing", "hardstanding", "dropped kerb", "crossover",
    "bin store", "cycle store", "decking", "porch",
]

SCORE_THRESHOLD = 40
PRIVATE_CLIENT_THRESHOLD = 45

SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    name              TEXT PRIMARY KEY,   -- PlanIt nationally unique id
    uid               TEXT,
    reference         TEXT,
    authority         TEXT,
    address           TEXT,
    postcode          TEXT,
    description       TEXT,
    app_type          TEXT,
    app_size          TEXT,
    app_state         TEXT,
    start_date        TEXT,
    decided_date      TEXT,
    last_changed      TEXT,
    lat               REAL,
    lng               REAL,
    url               TEXT,
    applicant_name    TEXT,
    applicant_company TEXT,
    agent_name        TEXT,
    agent_company     TEXT,
    agent_address     TEXT,
    score             INTEGER,
    lane              TEXT,
    postcode_tier     TEXT,
    reasons           TEXT,
    first_seen        TEXT,
    last_updated      TEXT,
    digested          INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_score ON applications(score DESC);
CREATE INDEX IF NOT EXISTS idx_digested ON applications(digested, score DESC);
CREATE INDEX IF NOT EXISTS idx_lane ON applications(lane, score DESC);

CREATE TABLE IF NOT EXISTS runs (
    started   TEXT,
    finished  TEXT,
    authority TEXT,
    fetched   INTEGER,
    kept      INTEGER,
    note      TEXT
);
"""


# --------------------------------------------------------------------------
# Fetch
# --------------------------------------------------------------------------

def fetch_page(authority, changed_days, page):
    """One page of the PlanIt delta query for an authority."""
    params = {
        "auth": authority,
        "changed": changed_days,
        "pg_sz": PAGE_SIZE,
        "page": page,
        "compress": "on",
        "select": "*",
    }
    url = f"{API}?{urllib.parse.urlencode(params)}"
    ua = os.environ.get("PLANIT_UA")
    if not ua:
        raise SystemExit(
            "PLANIT_UA not set. PlanIt returns 403 on an invalid User-Agent.\n"
            "Set something identifying, e.g.:\n"
            "  export PLANIT_UA='CredcoPlanningTracker/1.0 (dorant@credco.co.uk)'"
        )
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            retry = int(e.headers.get("Retry-After", 60))
            print(f"  rate limited, sleeping {retry}s", file=sys.stderr)
            time.sleep(retry)
            return fetch_page(authority, changed_days, page)
        if e.code == 403:
            raise SystemExit(
                "403 from PlanIt. Either the User-Agent is rejected or the IP is "
                "blocked. Contact andrew@planit.org.uk before retrying."
            )
        raise


def fetch_authority(authority, changed_days):
    """All pages for one authority."""
    out, page = [], 1
    while page <= MAX_PAGES:
        data = fetch_page(authority, changed_days, page)
        if "error" in data:
            print(f"  {authority}: API error: {data['error']}", file=sys.stderr)
            break
        records = data.get("records", [])
        out.extend(records)
        total = data.get("total", 0)
        to = data.get("to", 0)
        if not records or to >= total - 1:
            break
        page += 1
        time.sleep(REQUEST_PAUSE)
    return out


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

def other(rec, key):
    of = rec.get("other_fields") or {}
    if isinstance(of, str):
        try:
            of = json.loads(of)
        except Exception:
            return ""
    return (of.get(key) or "").strip()


def score(rec):
    """
    Return (score, reasons, lane).

    Two lanes. 'private_client' runs only inside prime postcodes and keeps
    substantial work to high-value homes that the developer lane would bin.
    'developer' is the SPV / units feed. Score of 0 means drop.
    """
    desc = (rec.get("description") or "").lower()
    tier = prime.tier(rec.get("postcode"), SUPER_PRIME, PRIME)

    if tier:
        sc, reasons = score_private_client(desc, rec, tier)
        if sc >= PRIVATE_CLIENT_THRESHOLD:
            return sc, reasons, "private_client"

    sc, reasons = score_developer(desc, rec, tier)
    return sc, reasons, "developer"


def score_private_client(desc, rec, tier):
    """HNW individual doing substantial work to their own home."""
    for kill in PRIVATE_CLIENT_KILL:
        if kill in desc:
            return 0, [f"excluded: {kill}"]

    total, reasons = 0, []
    for pattern, pts in PRIVATE_CLIENT_PATTERNS:
        if pattern in desc:
            total += pts
            reasons.append(pattern)

    if not total:
        return 0, ["no private client signal"]

    if tier == "super_prime":
        total += 30
        reasons.append("super-prime postcode")
    else:
        total += 15
        reasons.append("prime postcode")

    size = (rec.get("app_size") or "").lower()
    if size in ("large", "medium"):
        total += 15
        reasons.append(f"{size} scheme")

    state = (rec.get("app_state") or "").lower()
    if state == "permitted":
        total += 15
        reasons.append("permitted")
    elif state in ("rejected", "withdrawn"):
        return 0, [f"excluded state: {state}"]

    if other(rec, "agent_company"):
        total += 10
        reasons.append("agent identified")

    return total, reasons


def score_developer(desc, rec, tier):
    """SPV / developer doing units."""
    reasons = []
    for kill in KILL_PATTERNS:
        if kill in desc:
            return 0, [f"excluded: {kill}"]

    total = 0
    if tier == "super_prime":
        total += 20
        reasons.append("super-prime postcode")
    elif tier == "prime":
        total += 10
        reasons.append("prime postcode")

    for pattern, pts in POSITIVE_PATTERNS:
        if pattern in desc:
            total += pts
            reasons.append(pattern)

    size = (rec.get("app_size") or "").lower()
    if size == "large":
        total += 25
        reasons.append("large scheme")
    elif size == "medium":
        total += 20
        reasons.append("medium scheme")
    elif size == "small":
        total += 5

    app_type = rec.get("app_type") or ""
    if app_type in WANTED_TYPES:
        total += 10
    if app_type in ("Trees", "Advertising", "Telecoms"):
        return 0, [f"excluded type: {app_type}"]

    applicant = (other(rec, "applicant_name") + " " +
                 other(rec, "applicant_company")).lower()
    if any(h in applicant for h in CORPORATE_HINTS):
        total += 30
        reasons.append("corporate applicant")

    state = (rec.get("app_state") or "").lower()
    if state == "permitted":
        total += 20
        reasons.append("permitted")
    elif state == "undecided":
        total += 5
    elif state in ("rejected", "withdrawn"):
        return 0, [f"excluded state: {state}"]

    if other(rec, "agent_company"):
        total += 10
        reasons.append("agent identified")

    return total, reasons


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------

def qualifies(sc, lane):
    return sc >= (PRIVATE_CLIENT_THRESHOLD if lane == "private_client"
                  else SCORE_THRESHOLD)


def connect(path="tracker.db"):
    conn = db.connect(path)
    conn.executescript(SCHEMA)
    return conn


def upsert(conn, rec, sc, reasons, lane):
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    conn.execute("""
        INSERT INTO applications (
            name, uid, reference, authority, address, postcode, description,
            app_type, app_size, app_state, start_date, decided_date,
            last_changed, lat, lng, url, applicant_name, applicant_company,
            agent_name, agent_company, agent_address, score, lane,
            postcode_tier, reasons, first_seen, last_updated, digested
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)
        ON CONFLICT(name) DO UPDATE SET
            app_state    = excluded.app_state,
            lane         = excluded.lane,
            decided_date = excluded.decided_date,
            last_changed = excluded.last_changed,
            score        = excluded.score,
            reasons      = excluded.reasons,
            last_updated = excluded.last_updated,
            digested     = CASE
                WHEN applications.app_state != excluded.app_state THEN 0
                ELSE applications.digested END
    """, (
        rec.get("name"), rec.get("uid"), rec.get("reference"),
        rec.get("authority_name"), rec.get("address"), rec.get("postcode"),
        rec.get("description"), rec.get("app_type"), rec.get("app_size"),
        rec.get("app_state"), rec.get("start_date"), rec.get("decided_date"),
        rec.get("last_changed"), rec.get("lat"), rec.get("lng"),
        rec.get("url"),
        other(rec, "applicant_name"), other(rec, "applicant_company"),
        other(rec, "agent_name"), other(rec, "agent_company"),
        other(rec, "agent_address"),
        sc, lane, prime.tier(rec.get("postcode"), SUPER_PRIME, PRIME),
        ", ".join(reasons), now, now,
    ))


def ingest(conn, days=1):
    started = dt.datetime.now(dt.timezone.utc).isoformat()
    grand_fetched = grand_kept = 0

    for auth in AUTHORITIES:
        print(f"{auth}...", flush=True)
        try:
            records = fetch_authority(auth, days)
        except SystemExit:
            raise
        except Exception as e:
            print(f"  failed: {e}", file=sys.stderr)
            conn.execute(
                "INSERT INTO runs VALUES (?,?,?,?,?,?)",
                (started, dt.datetime.now(dt.timezone.utc).isoformat(),
                 auth, 0, 0, f"error: {e}"))
            conn.commit()
            continue

        kept = 0
        for rec in records:
            sc, reasons, lane = score(rec)
            if qualifies(sc, lane):
                upsert(conn, rec, sc, reasons, lane)
                kept += 1

        conn.execute(
            "INSERT INTO runs VALUES (?,?,?,?,?,?)",
            (started, dt.datetime.now(dt.timezone.utc).isoformat(),
             auth, len(records), kept, "ok"))
        conn.commit()
        print(f"  {len(records)} fetched, {kept} qualified")
        grand_fetched += len(records)
        grand_kept += kept
        time.sleep(REQUEST_PAUSE)

    print(f"\nTotal: {grand_fetched} fetched, {grand_kept} qualified")
    return grand_kept


# --------------------------------------------------------------------------
# Digest
# --------------------------------------------------------------------------

NAVY, MAGENTA = "#23293A", "#FF0082"


def build_digest(conn, limit=40):
    rows = conn.execute("""
        SELECT * FROM applications
        WHERE digested = 0
          AND ((lane = 'private_client' AND score >= ?)
            OR (lane = 'developer'      AND score >= ?))
        ORDER BY CASE lane WHEN 'private_client' THEN 0 ELSE 1 END,
                 score DESC, start_date DESC
        LIMIT ?
    """, (PRIVATE_CLIENT_THRESHOLD, SCORE_THRESHOLD, limit)).fetchall()
    cols = [d[0] for d in conn.execute("SELECT * FROM applications LIMIT 0").description]
    leads = [dict(zip(cols, r)) for r in rows]
    if not leads:
        return None, []

    today = dt.date.today().strftime("%d %B %Y")
    n_pc = sum(1 for l in leads if l["lane"] == "private_client")
    n_dev = len(leads) - n_pc
    e = html.escape

    cards = []
    for l in leads:
        contact = l["agent_company"] or l["applicant_company"] or l["applicant_name"] or ""
        pc = l["lane"] == "private_client"
        band = MAGENTA if pc else NAVY
        badge = ("PRIVATE CLIENT" if pc else "DEVELOPER")
        if l["postcode_tier"] == "super_prime":
            badge += " · SUPER-PRIME"
        elif l["postcode_tier"] == "prime":
            badge += " · PRIME"
        cards.append(f"""
        <tr><td style="padding:0 0 16px 0;">
          <table width="100%" cellpadding="0" cellspacing="0"
                 style="border:1px solid #E2E5EA;border-left:4px solid {band};
                        border-radius:4px;">
            <tr><td style="padding:16px 18px;font-family:Calibri,Arial,sans-serif;">
              <div style="font-size:10px;color:{band};font-weight:bold;
                          letter-spacing:.6px;">{badge}</div>
              <div style="font-size:11px;color:#8A8F9A;letter-spacing:.4px;
                          text-transform:uppercase;padding-top:3px;">
                {e(l['authority'] or '')} &nbsp;·&nbsp; {e(l['app_state'] or '')}
                &nbsp;·&nbsp; score {l['score']}
              </div>
              <div style="font-size:15px;color:{NAVY};font-weight:bold;padding:6px 0 2px;">
                {e(l['address'] or 'Address not stated')}
              </div>
              <div style="font-size:13px;color:#4A5060;line-height:1.45;">
                {e((l['description'] or '')[:260])}
              </div>
              <div style="font-size:12px;color:#4A5060;padding-top:8px;">
                <strong>Contact:</strong> {e(contact) or '<em>none listed</em>'}
              </div>
              <div style="font-size:11px;color:#8A8F9A;padding-top:8px;">
                Ref {e(l['reference'] or l['uid'] or '')} &nbsp;·&nbsp;
                Validated {e(l['start_date'] or '')} &nbsp;·&nbsp;
                <a href="{e(l['url'] or '#')}" style="color:{MAGENTA};">
                  View on council portal</a>
              </div>
            </td></tr>
          </table>
        </td></tr>""")

    body = f"""<!doctype html><html><body style="margin:0;background:#F4F5F7;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#F4F5F7;">
<tr><td align="center" style="padding:24px 12px;">
<table width="640" cellpadding="0" cellspacing="0" style="background:#ffffff;">
  <tr><td style="background:{NAVY};padding:20px 24px;
                 font-family:Calibri,Arial,sans-serif;">
    <div style="color:#ffffff;font-size:18px;font-weight:bold;">
      Planning Tracker</div>
    <div style="color:#A8AEBC;font-size:12px;padding-top:2px;">
      {n_pc} private client &nbsp;·&nbsp; {n_dev} developer
      &nbsp;·&nbsp; {today}</div>
  </td></tr>
  <tr><td style="padding:20px 24px;">
    <table width="100%" cellpadding="0" cellspacing="0">{''.join(cards)}</table>
  </td></tr>
  <tr><td style="padding:14px 24px;border-top:1px solid #E2E5EA;
                 font-family:Calibri,Arial,sans-serif;font-size:11px;color:#8A8F9A;">
    Sourced from public planning registers via PlanIt (planit.org.uk).
    Internal use only. Not to be forwarded externally.
  </td></tr>
</table></td></tr></table></body></html>"""

    return body, [l["name"] for l in leads]


def send(body):
    key = os.environ.get("RESEND_API_KEY")
    if not key:
        path = "digest.html"
        try:
            with open(path, "w") as f:
                f.write(body)
        except OSError:
            # Vercel's filesystem is read-only outside /tmp.
            path = os.path.join(tempfile.gettempdir(), "digest.html")
            with open(path, "w") as f:
                f.write(body)
        print(f"No RESEND_API_KEY. Written to {path}")
        return
    payload = json.dumps({
        "from": os.environ["DIGEST_FROM"],
        "to": os.environ["DIGEST_TO"].split(","),
        "subject": f"Planning Tracker — {dt.date.today():%d %b %Y}",
        "html": body,
    }).encode()
    req = urllib.request.Request(
        "https://api.resend.com/emails", data=payload,
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        print("Sent:", r.status)


# --------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["ingest", "digest", "run"])
    p.add_argument("--days", type=int, default=1)
    p.add_argument("--db", default="tracker.db")
    a = p.parse_args()

    conn = connect(a.db)

    if a.command in ("ingest", "run"):
        ingest(conn, a.days)

    if a.command in ("digest", "run"):
        body, names = build_digest(conn)
        if not body:
            print("Nothing new to send.")
            return
        send(body)
        conn.executemany(
            "UPDATE applications SET digested = 1 WHERE name = ?",
            [(n,) for n in names])
        conn.commit()
        print(f"Digest covered {len(names)} leads.")


if __name__ == "__main__":
    main()
