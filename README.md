# TimiCY Scrapers

Price-comparison data pipeline for Cypriot electronics retailers. Includes per-store scrapers and a Supabase/PostgreSQL ingestion pipeline that normalises, deduplicates, and upserts product and pricing data.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Open `.env` and fill in your real credentials before running anything.

## Environment variables

| Variable | Description |
|---|---|
| `DATABASE_URL` | Supabase Postgres connection string (`postgresql://user:password@host:port/dbname`) |

**Important:** `.env` is gitignored and must never be committed. The checked-in `.env.example` contains placeholder values only — no real credentials.
