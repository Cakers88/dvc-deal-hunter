# Broker adapters

Each module in this folder scrapes one broker and exposes:

```python
NAME = "brokername"                       # short, lowercase, unique

def fetch_listings(session) -> list[dict]:
    """Return EVERY DVC resale listing on the site (all resorts, all statuses)
    as normalized dicts (see hunter/model.py: blank(), FIELDS). Use as few
    requests as possible: prefer one index/API call that returns everything,
    paginate only if required."""

def fetch_detail(session, listing: dict) -> dict:   # optional
    """Fill in fields only available on the detail page (exact per-use-year
    points_available, dues_terms, dues_per_point, date_listed, expiration),
    set listing['detail_fetched'] = True, return it. Called only for new or
    changed listings that pass the resort filter, so it can be 1 request each."""
```

Rules
- Use only `session.get()/session.text()` from `hunter.http.PoliteSession` (rate limited).
- Never log in, submit forms, or bypass bot protection. If a site blocks us, return [] and log a warning.
- `resort` via `hunter.resorts.normalize_resort`, `use_year` via `month_num`, `availability` via `model.classify_status`.
- `points_available` buckets are keyed by the use year START (`"2026-08"`). Use `model.parse_availability_lines` when the site prints "August 2026 - 220 points".
- Keep anything else useful in `extra` (closing cost, notes, "can close after", etc.).

See `dvcresalemarket.py` for the reference implementation.
