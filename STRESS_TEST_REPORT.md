# Stress Test Report

Generated 2026-11-03 18:57:04 by `tests/stress_test.py`.

**Verdict: PASS**

| Setting | Value |
|---|---|
| Mock posts | 10,000 |
| Concurrent workers | 50 |
| Dashboard requests | 1,000 (20 in parallel) |
| Python | 3.13.18 on Linux x86_64 |
| SQLite | 3.53.1 |
| Database | temporary file, 4.9 MB after inflation |

## 1. Database inflation

Inserted 10,000 posts in 0.10 s (101,133 rows/s), one transaction.

| Status | Rows |
|---|---|
| draft | 3,000 |
| approved | 956 |
| scheduled | 1,428 |
| published | 4,098 |
| rejected | 518 |
| with_error | 271 |

## 2. Query timings (against the inflated database)

| Query | Rows returned | Mean | p95 | Max |
|---|---|---|---|---|
| `stats()  (dashboard tiles)` | 1 | 7.5 ms | 7.8 ms | 8.9 ms |
| `list_posts(limit=200)` | 200 | 3.5 ms | 3.9 ms | 4.5 ms |
| `list_posts(search='brunch')` | 200 | 4.3 ms | 4.7 ms | 4.7 ms |
| `queue()  (approval page)` | 70 | 9.2 ms | 11.4 ms | 11.5 ms |
| `calendar_month(2026, 6)` | 1 | 8.7 ms | 9.0 ms | 10.5 ms |
| `list_errors()` | 100 | 2.1 ms | 2.4 ms | 2.5 ms |
| `list_runs()` | 0 | 0.3 ms | 0.5 ms | 0.9 ms |
| `publisher.fetch_due_posts()` | 2,162 | 36.3 ms | 38.2 ms | 49.0 ms |
| `get_post(random id)` | 1 | 0.6 ms | 0.6 ms | 5.9 ms |

## 3. Concurrency and race conditions

**Read/write storm.** 50 threads ran 2,000 mixed operations (stats, list, edit caption, approve/un-approve, clear error) in 13.30 s.

| Check | Result |
|---|---|
| "database is locked" errors | 0 |
| Other errors | 0  |

**Publisher race.** 50 publisher workers fetched the same 200 approved posts at the same moment and tried to publish them to a fake Facebook API (32.63 s).

| Check | Result |
|---|---|
| Posts published | 200 of 200 |
| API calls made | 200 (ideal: 200) |
| Double publishes | 0 |
| "database is locked" errors | 0 |

**Overlapping orchestrator runs.** 50 threads called `run_pipeline()` at once.

| Check | Result |
|---|---|
| Ran | 1 (expected 1: the lock lets one through) |
| Skipped cleanly | 49 |
| Crashed | 0 |

## 4. Dashboard benchmark

1,000 requests, 20 at a time, against a real uvicorn server with the inflated database: 13.16 s total, **76 requests/s**.

| Endpoint | Requests | p50 | p95 | p99 | Max | Size | Errors |
|---|---|---|---|---|---|---|---|
| `/` | 167 | 108.2 ms | 270.0 ms | 322.5 ms | 343.0 ms | 12 KB | 0 |
| `/queue` | 167 | 159.9 ms | 309.3 ms | 385.6 ms | 515.4 ms | 123 KB | 0 |
| `/api/posts` | 167 | 119.0 ms | 262.5 ms | 330.8 ms | 375.7 ms | 72 KB | 0 |
| `/api/stats` | 167 | 109.4 ms | 214.5 ms | 322.3 ms | 411.9 ms | 4 KB | 0 |
| `/calendar?year=2026&month=6` | 166 | 108.7 ms | 249.3 ms | 318.3 ms | 360.1 ms | 63 KB | 0 |
| `/logs` | 166 | 114.1 ms | 272.5 ms | 303.9 ms | 446.2 ms | 60 KB | 0 |

## 5. Memory

| Measure | Value |
|---|---|
| Stress-test process peak RSS | 384 MB |
| Python heap peak (tracemalloc) | 74.2 MB |
| Dashboard server peak RSS | 118 MB |

## 6. What the stress test changed in the code

Findings from the first run, and the fixes now in place:

- **SQLite locking.** Concurrent writers raised `database is locked`. Fixed by enabling WAL journal mode (readers no longer block writers) and a 30 s busy timeout on every connection (`config/db.py`).
- **Double publishing.** Two publisher processes could pick the same approved post before either saved a platform ID. Fixed with an atomic claim (`claimed_at` column): a worker only publishes a post it managed to claim, and stale claims expire after 10 minutes (`src/publisher.py`).
- **Approval queue page size.** The queue rendered every draft on one page. With thousands of drafts that was multi-megabyte HTML. Fixed with paging, 25 cards per section (`src/web/queries.py`).
- **Indexes.** Added indexes on `status`, `scheduled_time` and `last_error` so the dashboard counts and the publisher's due-post query stay fast as the table grows.

## 7. Honest reading of these numbers

- This machine, SQLite and a single uvicorn worker are the whole stack. The numbers show the tool is comfortable far beyond four clients, not that it is a multi-user web service.
- The benchmark hits a server on the same machine. Real page loads add browser rendering and the Tailwind CDN download.
- No real API was called. Meta, TikTok and OpenAI latency and rate limits will dominate a real run and are not measured here.
