# Social Automation Pipeline

Generates social media posts with AI, creates and hosts images, lets you
approve each post on a local dashboard, and publishes approved posts to
Facebook, Instagram and TikTok.

```
topic ──▶ generator ──▶ image_generator ──▶ uploader ──▶ [you approve] ──▶ publisher
          caption +      PNG via OpenAI      JPEG on       dashboard         Facebook
          hashtags       image API           Imgur / R2                      Instagram
                                                                             TikTok
```

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env          # then fill in your keys
python3 -m src.main keygen    # paste the APP_SECRET_KEY line into .env
python3 config/db.py          # creates data/scheduler.db
```

## Daily use

```bash
python3 -m src.main web                       # dashboard at http://127.0.0.1:8000
python3 -m src.main loop                      # background runner, every 5 minutes
```

Keep both running. Create drafts on the dashboard (AI or by hand), approve
them on the **Approval queue** page, and the loop publishes them on its next
pass. Scheduled posts are published at their scheduled time.

One-off commands:

```bash
python3 -m src.main run --topic "Summer menu launch"   # generate + images + upload + publish
python3 -m src.main run --steps images,upload          # only some steps
python3 -m src.main run --dry-run                      # show what would happen
python3 -m src.generator "Topic" --platform facebook --business "Taverna Nikos"
python3 -m src.publisher --id 3 --platforms facebook
```

## Post workflow

| status      | meaning                                                        |
|-------------|----------------------------------------------------------------|
| `draft`     | created, waiting for review. Never published automatically.   |
| `approved`  | publish on the next pipeline run                               |
| `scheduled` | approved with a time; publish when `scheduled_time` is reached |
| `published` | done on every requested platform. Read-only.                   |
| `rejected`  | never publish                                                  |

Per-platform post IDs are stored the moment each platform succeeds, so a
retry never double-posts. Any failure is written to the post's `last_error`
and shown on the **Logs** page.

## Credentials

Every script reads tokens through `src/security/secrets.py`:

1. `.env` wins if the value is set and not a placeholder.
2. Otherwise the encrypted `credentials` table, filled from the dashboard
   **Settings** page. Values are AES-256-GCM encrypted with `APP_SECRET_KEY`.

The key lives in `.env` next to the database, so this protects a copied or
backed-up `scheduler.db`, not a fully compromised machine.

## Platform notes (read these)

- **Instagram** accepts JPEG only, from a public URL. The uploader handles both.
- **TikTok** only pulls media from a domain you verified in its developer
  portal, so Imgur does not work for TikTok. Use `MEDIA_HOST=r2` with a
  Cloudflare R2 bucket behind your own domain. Unaudited TikTok apps can only
  post privately (`TIKTOK_PRIVACY_LEVEL=SELF_ONLY`, the default). TikTok
  access tokens expire every 24 hours.
- **Imgur** forbids commercial hosting in its terms. Fine for testing, use R2
  for client work.
- **Images** from the OpenAI image API expire after about an hour. The uploader
  stores a permanent copy, so run it before publishing.
- The dashboard has **no login** and binds to 127.0.0.1. Do not expose it.

## Layout

```
config/db.py            SQLite connection, schema, migrations
src/generator.py        topic -> caption, hashtags, image prompt (OpenAI)
src/image_generator.py  image prompt -> PNG (OpenAI image API)
src/uploader.py         PNG -> JPEG -> Imgur or Cloudflare R2
src/publisher.py        approved posts -> Facebook, Instagram, TikTok
src/main.py             orchestrator: run / loop / generate / web / keygen
src/security/           AES-256-GCM crypto and the secrets store
src/web/                FastAPI dashboard (templates/, static/)
tests/                  76 tests, no network needed: python3 -m unittest discover -s tests
```
