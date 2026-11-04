# Social Automation Pipeline

Generates social media posts with AI, creates and hosts images, lets you
approve each post on a local dashboard, and publishes approved posts to
Facebook, Instagram and TikTok.

```
                                   image posts
topic ──▶ generator ──▶ image_generator ──────────────────▶ uploader ──▶ [approve] ──▶ publisher
          caption +      PNG via OpenAI                      JPEG/MP4      dashboard     Facebook
          hashtags       image API      ╲  video posts       to Imgur/R2                 Instagram
                                         ╲▶ audio_generator ─▶ video_compiler ─▶ (same)  TikTok
                                            caption -> speech  image + speech -> 1080x1920 MP4
                                            (OpenAI/ElevenLabs) with timed captions + music
```

## Quick start (one click)

```bash
./setup.sh        # Mac / Linux
setup.bat         # Windows (double-click works too)
```

The launcher checks for Docker and Python, creates `.env` with random
secrets filled in, starts the platform (Docker with PostgreSQL if Docker is
running, otherwise Python + SQLite), and prints the dashboard address. Open
it, create your account, then follow the **Key wizard** on the API settings
page to get your OpenAI and Meta keys. `./setup.sh --stop` stops everything.

## Manual setup

```bash
pip install -r requirements.txt
cp .env.example .env          # then fill in your keys
python3 -m src.main keygen    # paste the APP_SECRET_KEY line into .env
python3 -m config.migrate upgrade   # creates data/scheduler.db (SQLite)
```

## Daily use

```bash
python3 -m src.main web                       # dashboard at http://127.0.0.1:8000
python3 -m src.main loop                      # background runner, every 5 minutes (WORKER_INTERVAL_MINUTES in .env)
```

Keep both running. Open the dashboard, **sign up** (the first account is
yours), create drafts (AI, by hand, or drop in a finished video), approve them
on the **Approval queue** page, and the loop publishes them on its next pass.
Scheduled posts are published at their scheduled time.

## Accounts, plans and tenancy

Every user has their own posts, pipeline runs, media files and encrypted API
keys. Nothing is shared between accounts: pages, the JSON API and the
`/files/...` routes all check ownership and answer 404 for anyone else's data.
Sessions are JWTs in an httpOnly cookie (or a Bearer token from
`POST /api/auth/token`); passwords are bcrypt hashed.

| Plan       | Posts / month | Custom video uploads | AI video reels |
|------------|---------------|----------------------|----------------|
| Free       | 5             | no                   | no             |
| Pro        | 50            | yes                  | no             |
| Enterprise | unlimited     | yes                  | yes            |

Plans are enforced before anything expensive runs. Stripe Checkout and the
customer portal are wired up (`STRIPE_*` in `.env`, webhook at
`/api/billing/webhook`); without Stripe, set plans by hand:

```bash
python3 -m src.billing.stripe_integration set-tier you@example.com enterprise
```

API keys entered on a user's Settings page are used for that user's posts.
Values in `.env` act as platform defaults for everyone (for example one
OpenAI key the operator pays for).

**Custom video uploads**: drop an MP4/MOV/WEBM on the dashboard. It skips the
AI generator, voice-over and renderer and goes straight to hosting and the
approval queue. Files are checked with ffprobe and capped at `MAX_UPLOAD_MB`.

## Database

SQLite by default. Set `DATABASE_URL=postgresql://...` for PostgreSQL; the
same code and migrations run on both. `python3 -m config.migrate status`
shows applied migrations. Old single-user posts can be handed to an account
with `python3 -m config.migrate claim-orphans you@example.com`.

The test suite runs on both backends:

```bash
python3 -m unittest discover -s tests
TEST_DATABASE_URL=postgresql://user:pass@localhost:5432/social_test tests/run_postgres.sh
```

## Docker

`docker compose up -d --build` starts PostgreSQL, the dashboard and the
background worker (see `docker-compose.yml`). Put a TLS reverse proxy in
front before exposing it to the internet, and set `COOKIE_SECURE=1`.

One-off commands:

```bash
python3 -m src.main run --topic "Summer menu launch"   # generate + images + upload + publish
python3 -m src.main run --steps images,upload          # only some steps
python3 -m src.main run --dry-run                      # show what would happen
python3 -m src.generator "Topic" --platform facebook --business "Taverna Nikos"
python3 -m src.publisher --id 3 --platforms facebook
```

## Media library: your own footage becomes posts

Every account has a folder, `data/library/user_<id>/`, also reachable from the
**Media library** page. Drop photos and clips there (or upload them on the
page). The tool looks at each file with a vision model, writes a caption and
hashtags in the brand's voice (set on the **Profile** page: business name,
brief, language), and makes posts from the least-used material:

- a photo becomes an image post,
- a short clip becomes a video post as it is,
- several clips become a vertical montage with background music and a
  title.

Set **Automatic posts per day** on the Profile page and the background loop
creates that many drafts each day. Everything goes to the approval queue;
nothing is posted without your approval. `python3 -m src.library plan --user 1 --count 2`
does the same from the command line.

## Queue, hashtags, recycling, notifications

- **Posting slots** (Profile page): define times such as
  `Mon,Tue,Wed,Thu,Fri 09:00,18:30; Sat,Sun 11:00`. "Approve: next slot" on
  any post gives it the next free time, like a Buffer queue.
- **Hashtags in the first comment** (Profile page): the caption stays clean
  and the hashtags are posted as the first comment on Facebook and Instagram.
- **Evergreen** (post page): a published post marked evergreen comes back as
  a draft every N days.
- **Telegram notifications**: new drafts waiting, published, failed. Keys on
  the API settings page.

## TikTok (connect once, tokens refresh by themselves)

Settings > **Connect TikTok** after saving the app's client key and secret
(wizard section 7). Videos are uploaded as files, so no domain verification.
Until TikTok audits your app its posts can only be private, so the default
mode (`TIKTOK_POST_MODE=inbox`) drops each video into your TikTok inbox as a
draft and you publish it from the TikTok app with one tap. After the audit,
set `TIKTOK_POST_MODE=direct` and `TIKTOK_PRIVACY_LEVEL=PUBLIC_TO_EVERYONE`.

## More channels

- **Threads**: a Threads app with `threads_basic` + `threads_content_publish`;
  text posts work without hosting, photos and videos need a hosted link.
- **YouTube Shorts**: a Google Cloud OAuth client, then "Connect YouTube" on
  the API settings page. Google's free quota allows about six uploads a day.
- Both are platform checkboxes on every post, like the others.

## Your own clips: auto-captions, CSV import, best times

- **Auto-captions** (Profile page): uploaded and library clips get the speech
  transcribed (OpenAI Whisper, or Gemini on the free tier) and burned in as
  word-group captions before publishing.
- **CSV import** (All posts page): plan a month in a spreadsheet and import
  it; a template is provided.
- **Best times** (Profile page): one click reads when your followers are
  online from Meta and fills in the posting slots.
- **Content pillars** (Profile page): the AI rotates through them so posts
  are varied.

## Keeping it healthy

- The dashboard warns when the **background worker** has not reported in
  for 15 minutes, and when a connected Page's **Facebook access is invalid
  or about to expire** (checked daily against Meta).
- A **daily backup** of the SQLite database goes to `data/backups/`
  (14 kept). PostgreSQL users use `pg_dump`.
- A post that fails to publish waits 10, 20, 40, 80 and 160 minutes between
  retries, stops after 5 and waits for you ("Publish again" resets it). When
  Facebook answers with its spam block (error 368, "we limit how often you
  can post") the tool waits 6 hours; do not keep re-approving, the block
  normally clears within a day.
- Browser forms are protected against cross-site request forgery; API
  clients with a Bearer token are unaffected. `CSRF_DISABLED=1` only for tests.
- **Captions are checked before publishing** against each platform's limits
  (Instagram 2,200 characters / 30 hashtags / 20 mentions, Threads 500).
  The post card shows a warning and the post is not sent until it is fixed.
- **Meta errors are translated**: an expired token, a rejected video format,
  the daily publishing cap or a rate limit come back as a plain sentence with
  the fix, and errors Meta documents as temporary are retried on a later
  cycle (status checks tolerate a few glitches; `media_publish` retries
  "media not ready").
- Instagram image posts carry **alt text** (title + first sentence) and
  pictures made by an AI image generator are **labelled as AI-generated**
  on Instagram, as Meta's publishing API now allows. Your own footage and
  stock photos are not labelled.
- The Graph API version is `META_GRAPH_VERSION` (default v25.0). Meta retires
  a version about two years after release; bump it in `.env` when Meta
  announces one.

## Carousels (several photos in one post)

Media library: tick 2 to 10 photos and click **Create carousel post**. The
draft takes the AI caption of the first photo; edit it, approve it, and it
goes out as an Instagram carousel and a Facebook multi-photo post (the other
channels do not take photo carousels). Instagram needs hosting
(`MEDIA_HOST=r2`) for every photo; Facebook also works from local files.

## Client review links (approval without a login)

Settings > **Client review links**: create one link per client (optionally
limited to one connected Page, optionally expiring). The client opens it on
their phone, sees the posts waiting for approval with the media, and clicks
**Approve** (the post joins the queue) or **Ask for changes** with a note.
The note shows on the post card in your queue, and Telegram tells you when
they decide. Links are revocable; nothing else on the dashboard is reachable
from them.

## Ideas & dates

The **Ideas & dates** page lists Greek public holidays and Orthodox movable
feasts (computed for any year), Carnival and Tsiknopempti, and the
international days hospitality businesses post about, each with a one-click
draft. "Suggest ideas" asks the AI for two weeks of topics in the brand's
voice, aware of what is coming up.

## Performance and client reports

The `metrics` step pulls likes, comments, shares, saves, reach and views for
published posts from Meta once a day. The **Performance** page shows a month
per Page with top posts; **Client report** opens a clean printable page (save
as PDF) and **CSV** exports the numbers. `python3 -m src.analytics fetch --force`
refreshes everything now.

## AI providers

The tool works with whichever keys you have. **Gemini** (free tier) covers
captions and photo/video understanding; **Groq** (free tier) is a fast
text-only alternative; **Cloudflare Workers AI** (small free daily allowance)
or **OpenAI** (paid) generate images, and **Pexels** (free) supplies stock
photos when no image provider is set; OpenAI or ElevenLabs make voice-overs.
Keys go in `.env` or per user on the API settings page.

## Video posts (TikTok / Reels)

Choose **Video** when creating a draft (dashboard, or `--media-type video`).
The pipeline then also runs:

- `src/video/audio_generator.py`: reads the caption aloud with OpenAI TTS
  (or ElevenLabs), saving `data/audio/post_<id>.mp3`. Hashtags, links and
  emojis are stripped from the spoken text.
- `src/video/video_compiler.py`: MoviePy render of the post image (or a random
  clip from `data/stock/`) scaled to 1080x1920, looped to the speech length,
  with 3-word captions timed to the audio and a quiet track from
  `data/bg_music/` if one exists. Output: `data/videos/post_<id>.mp4`.

Caption timing is estimated from word lengths. Set `CAPTION_SYNC=whisper`
for real word timestamps (one extra OpenAI transcription call per video).
Rendering is slow: roughly 5-10x the video length on a laptop.

Video posts need **FFmpeg** installed (`apt install ffmpeg` / `brew install ffmpeg`)
and a host that accepts MP4, which means `MEDIA_HOST=r2`. Facebook gets a
**Reel** when the video is vertical (Reels Publishing API, more reach than a
feed video; `FB_VERTICAL_AS_REEL=0` turns this off) and a feed video otherwise,
Instagram a Reel, TikTok a video.

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
- The dashboard binds to 127.0.0.1 by default. Before exposing it: HTTPS,
  `COOKIE_SECURE=1`, a real `JWT_SECRET`, and backups of the database.
  There is no email verification or password reset yet.

## Layout

```
config/db.py            SQLite / PostgreSQL connection layer
config/migrations.py    schema migrations (both backends); config/migrate.py CLI
src/generator.py        topic -> caption, hashtags, image prompt (OpenAI)
src/image_generator.py  image prompt -> PNG (OpenAI image API)
src/uploader.py         PNG -> JPEG -> Imgur or Cloudflare R2
src/publisher.py        approved posts -> Facebook, Instagram, TikTok
src/video/              audio_generator.py (TTS) and video_compiler.py (MoviePy)
src/main.py             orchestrator: run / loop / generate / web / keygen
src/security/           AES-256-GCM crypto and the per-user secrets store
src/billing/            tiers, usage limits, Stripe integration
src/web/                FastAPI dashboard: app.py, auth.py, queries.py, templates/, static/
src/ai/providers.py     Gemini / OpenAI / Cloudflare behind one interface (text, vision, image)
src/library.py          media library: scan, analyze, plan posts, montage
src/analytics.py        post metrics from Meta, Performance page data, client reports
src/slots.py            posting time slots ("next slot")
src/stock.py            Pexels stock photos and clips
src/notify.py           Telegram notifications
src/recycle.py          evergreen recycling
src/youtube.py          YouTube Shorts OAuth and resumable upload
src/subtitles.py        auto-captions for own clips
src/importer.py         CSV import of posts
src/ideas.py            holidays calendar and AI topic suggestions
src/ops.py              worker heartbeat, backups, token health
src/pages.py            connected Facebook Pages with their Instagram accounts
tests/                  unit + integration tests, no network needed: python3 -m unittest discover -s tests
```
