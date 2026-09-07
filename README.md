# Planning Tracker

Daily delta pull from PlanIt, scored for commercial finance relevance, emailed as a digest.

## Coverage

Six region/county queries covering roughly 100 LPAs: London, Essex, Hertfordshire,
Kent, Surrey, Buckinghamshire. PlanIt includes sub-areas by default.

Verify the names resolve before the first live run:

    curl -A "$PLANIT_UA" "https://www.planit.org.uk/api/areas/json?auths=Essex&pg_sz=20"

Add Berkshire and Bedfordshire once the base list is confirmed working.

## Run locally

    export PLANIT_UA='CredcoPlanningTracker/1.0 (dorant@credco.co.uk)'
    python3 tracker.py ingest --days 7    # backfill a week, check output
    python3 tracker.py digest             # writes digest.html if no send key
    python3 tracker.py run                # both, and send

Sending needs `RESEND_API_KEY`, `DIGEST_FROM` (verified domain), `DIGEST_TO`.

Local runs use a `tracker.db` SQLite file by default (gitignored). Set
`POSTGRES_URL` in your shell to point the same CLI at Postgres instead —
useful for testing against the production database before a deploy.

## Scoring — two lanes

**Private client** (threshold 45). Runs only inside prime postcodes. Keeps
substantial work to high-value homes: basement digs, replacement dwellings,
pool houses, staff accommodation, comprehensive refurbishment. The developer
lane would bin all of these as householder work, which is why the lane exists.

**Developer** (threshold 40). SPVs and units. Kill list drops extensions, loft
conversions, dormers, trees, signage, conditions, rejected and withdrawn.
Scored on description keywords, scheme size, application type, corporate
applicant and decision status. Prime postcodes get a bonus but are not required.

A basement excavation in SW10 scores 200 and lands in the private client lane.
The same words in IG11 score 20 and drop. That discrimination is the point.

Note that private client leads are frequently **regulated** — the security is
the borrower's own home. Route accordingly.

## Prime postcodes

`prime.py` ships a curated seed list of districts across London, Surrey, Berks,
Bucks, Herts, Essex and Kent, split into super-prime and prime tiers.

Replace it with evidence as soon as you can. Download Land Registry Price Paid
Data (free, Open Government Licence, monthly) from
https://www.gov.uk/guidance/about-the-price-paid-data then:

    python3 prime.py pp-2025-2026.csv

That computes median sale price per postcode district over the file's range,
excluding flats, new builds and non-standard transfers, and writes
`prime_postcodes.json`. The tracker picks it up automatically. Re-run quarterly.

Tune `SCORE_THRESHOLD`, `PRIVATE_CLIENT_THRESHOLD` and the pattern lists after
the first week of live output. Expect to add kill patterns — that is where the
noise is.

## Deployment — Vercel + Postgres

`vercel.json` schedules `/api/run` once daily, early morning:

    { "crons": [{ "path": "/api/run", "schedule": "0 6 * * *" }] }

SQLite does not persist on Vercel's filesystem, so the deployed function
(`api/run.py`, `db.py`) uses Postgres instead — the schema and queries in
`tracker.py` are written to be portable between the two, so nothing else
changes. The local CLI still uses SQLite by default; see "Run locally" above
to point it at Postgres too.

**Setup:**

1. Push this repo to GitHub, then import it in Vercel (New Project → your repo).
2. Attach a Postgres database — Storage tab → Create Database → Postgres (or
   any Postgres provider). Vercel injects `POSTGRES_URL` into the project's
   environment automatically once attached; `db.py` picks it up with no
   further config.
3. Set environment variables (Project → Settings → Environment Variables):
   - `PLANIT_UA` — e.g. `CredcoPlanningTracker/1.0 (dorant@credco.co.uk)`
   - `RESEND_API_KEY`, `DIGEST_FROM` (verified domain), `DIGEST_TO`
   - `CRON_SECRET` — any random string. Vercel Cron Jobs automatically send
     it as `Authorization: Bearer <value>` on scheduled calls, and `api/run.py`
     rejects any request that doesn't carry it. Generate one with
     `openssl rand -hex 32`.
4. Deploy. The cron fires on Vercel's schedule; trigger it manually to test
   with `curl -H "Authorization: Bearer $CRON_SECRET" https://<project>.vercel.app/api/run`.

The Hobby plan caps function duration at 60s; `vercel.json` requests 300s
(`maxDuration`), which needs a Pro plan or higher. A daily 1-day delta is
normally quick, but a first backfill or an unusually busy day across six
regions can run long — if that matters, either upgrade the plan or split
`AUTHORITIES` across separate cron-triggered functions.

Alternative: everything above is Vercel-specific. `tracker.py`'s CLI needs
no serverless glue at all — a small always-on box (a £4/month VPS) with a
crontab calling `python3 tracker.py run` is genuinely adequate for this
volume, and sidesteps the Postgres requirement entirely (plain SQLite is
fine on a persistent disk).

## Before going live

- Email andrew@planit.org.uk. The API is rate limited and the spec says to
  enquire. Say who you are, six region-level queries, once daily. It is a free
  service run by one person and the goodwill is worth more than the data.
- Keep `REQUEST_PAUSE` at 2 seconds or higher.
- Digest is internal only. It contains applicant names from the public register.
- Retention: add a purge for records older than 12 months that were never
  actioned. Holding stale personal data indefinitely is the weak point in any
  GDPR position.

## Not included

Companies House enrichment and agent web lookup are the obvious next layer.
Both are straightforward once the base feed is proven and tuned.
