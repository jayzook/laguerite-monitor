# La Guérite Cannes — reservation availability monitor

Watches the official [La Guérite Cannes booking page](https://www.sevenrooms.com/reservations/lagueritecannes)
and emails you the moment a **genuinely bookable** table appears for your date,
party size and time window.

Default target: **3 September 2026, 6 people, 14:00–16:00 (Europe/Paris)**.

---

## What it actually checks (read this first)

SevenRooms returns two different kinds of time slot, and they are **not** the same thing:

| Slot type | What it means | Counted as availability? |
|---|---|---|
| `book` | A real table you can confirm right now. Renders as a clickable time button. | **Yes** |
| `request` | Only lets you *submit a request* the restaurant may decline. | **No** |

The monitor only alerts on `book`. This is deliberate — requirement "do not consider
the check successful unless an actual bookable reservation slot is visible".

### Current state of your target date

At the time this was built, **3 Sep 2026 for 6 guests** offers:

- `12:00` and `12:15` — **bookable** (First Sitting)
- `12:30` through `17:30` — **request only**, including your whole 14:00–16:00 window

So the monitor will sit quietly and correctly report *no availability* until a
second-sitting table is actually released. For comparison, 8 Sep 2026 currently
*does* have bookable 14:00 / 14:30 / 14:45 / 15:15 / 15:30 slots — which is how the
detection logic was verified against live data.

The booking page also notes that **DB Members get priority for the second seating**
(the 14:00–16:00 one). Membership enquiries: `cannes@restaurantlaguerite.com`.

---

## How it works

1. **Primary — the public availability endpoint.**
   The monitor calls `https://www.sevenrooms.com/api-yoa/availability/widget/range`,
   which is the exact request the booking widget makes from your browser. No login,
   no token, no CAPTCHA, and `/api-yoa/` is not disallowed by
   [SevenRooms' robots.txt](https://www.sevenrooms.com/robots.txt). One small request
   per check, at a randomised interval.

2. **Fallback — Playwright** (optional, off by default).
   Loads the real booking page in headless Chromium and drives the form exactly as a
   person would: set guests → pick the date → press Search. It then reads availability
   two ways and cross-checks them:
   - the availability XHR the widget itself fires, parsed by the *same* parser as (1);
   - the rendered `sr-timeslot-button` elements, filtered by their `data-date`
     attribute so the page's "Other dates with availability" carousel can never be
     mistaken for your date.

No CAPTCHA solving, no login bypass, no screen coordinates, no image recognition.

### Robustness

Detection keys off SevenRooms' `data-test` hooks and JSON field names rather than CSS
classes or pixel positions, so restyling the site does not break it. If the monitor
reaches the site but genuinely **cannot tell** what is available — the JSON shape
changed, the slot vocabulary is unfamiliar, the form no longer drives — it treats that
as an error and **emails you** instead of silently reporting "no availability" forever.

---

## Quick start

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux

pip install -r requirements.txt

copy .env.example .env          # Windows
# cp .env.example .env          # macOS / Linux
```

Now edit `.env` and add **one** notification channel (see below), then:

```bash
python -m laguerite.monitor --test-notification   # confirm alerts reach you
python -m laguerite.monitor --once                # one real check
python -m laguerite.monitor                       # run forever
```

---

## Notification setup

Configure **at least one** channel. If you set several, all of them fire.

`.env` ships configured for **Telegram** (`NOTIFY_CHANNELS=telegram`).

### Option 1 — Telegram (current default; free, instant, no domain or DNS)

"BotFather" is itself a Telegram bot — you create your bot by *chatting* with it.

**Step 1 — create the bot.** In Telegram, search for `@BotFather` (blue checkmark) and
open it. Press **START**, then send:

```
/newbot
```

It asks two questions:

| BotFather asks | You reply | Notes |
|---|---|---|
| "Choose a name for your bot" | `La Guerite Watcher` | Any display name. Spaces fine. |
| "Choose a username… must end in `bot`" | `laguerite_watch_bot` | Must be globally unique. If taken, add digits and retry. |

It then replies **"Done! Congratulations…"** with a line like:

```
8123456789:AAHxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

That whole string is your token. Copy it.

**Step 2 — say hello to your bot.** In BotFather's reply, tap the `t.me/your_bot_name`
link, press **START**, and send it any message.
*(Telegram forbids a bot from messaging you until you message it first — skipping this
is the single most common reason setup fails.)*

**Step 3 — one command finishes the job:**

```bash
python -m laguerite.monitor --telegram-setup 8123456789:AAHxxxxxxxx
```

It validates the token, finds your chat ID, and **writes both values into `.env` for
you** (other settings and comments are preserved):

```
Bot token is valid — you are talking to @laguerite_watch_bot
Found 1 chat(s) that have messaged this bot:
    987654321        (yourname)

Saved to .env (updated: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
You're done. Now run:
    python -m laguerite.monitor --test-notification
```

**Step 4 — test:** `python -m laguerite.monitor --test-notification`

> Telegram alerts are sent as **plain text** on purpose. Markdown mode would read the
> underscores in the booking URL (`default_party_size`, `default_time`) as italic
> markers and break the link. Telegram auto-links bare URLs anyway.

### Option 2 — Email via Resend

1. Sign up at [resend.com](https://resend.com), then **API Keys → Create API Key**.
2. In `.env`, set `NOTIFY_CHANNELS=email` plus:
   ```ini
   RESEND_API_KEY=re_your_key_here
   NOTIFY_EMAIL_TO=you@example.com
   NOTIFY_EMAIL_FROM=onboarding@resend.dev
   ```

> **Resend limitation:** the shared `onboarding@resend.dev` sender can only deliver to
> the email address your Resend account is registered with. To send anywhere else,
> verify a domain (**Domains → Add Domain**) and set
> `NOTIFY_EMAIL_FROM=alerts@yourdomain.com`.

### Option 3 — Pushover

Buy the app, then from your [dashboard](https://pushover.net) copy your user key and
create an application token:

```ini
PUSHOVER_TOKEN=your_app_token
PUSHOVER_USER_KEY=your_user_key
```

### Testing without any credentials

`NOTIFY_CHANNELS=console` prints notifications to the log instead of sending them.
Useful for exercising the whole pipeline before you sign up for anything.

---

## Commands

| Command | What it does |
|---|---|
| `python -m laguerite.monitor` | Run continuously (this is the normal mode). |
| `python -m laguerite.monitor --once` | Single check, then exit. |
| `python -m laguerite.monitor --telegram-setup` | Validate your bot token and print your Telegram chat ID. |
| `python -m laguerite.monitor --test-notification` | Send a test alert to every configured channel. |
| `python -m laguerite.monitor --simulate "14:30,15:00"` | Pretend those times are bookable and run the real notify path. |
| `python -m laguerite.monitor --browser` | Force the Playwright browser check. |
| `python -m laguerite.monitor --check-config` | Print resolved settings (secrets masked). |
| `python -m laguerite.monitor --status` | **Is it alive?** Reports ALIVE or STALE from the last check time. |
| `python -m laguerite.monitor --show-state` | Show what it currently remembers. |
| `python -m laguerite.monitor --reset-state` | Forget remembered slots (re-alerts next time). |
| `python -m unittest discover -s tests` | Run the 51-test suite. |

---

## Configuration

Everything lives in `.env` (or real environment variables on your host).

| Variable | Default | Meaning |
|---|---|---|
| `RESTAURANT` | `La Guerite Cannes` | Display name in alerts. |
| `DATE` | `2026-09-03` | Target date, `YYYY-MM-DD`. |
| `PARTY_SIZE` | `6` | Number of guests. |
| `EARLIEST_TIME` | `14:00` | Start of the acceptable window (venue local time, inclusive). |
| `LATEST_TIME` | `16:00` | End of the window (inclusive). |
| `MIN_CHECK_INTERVAL_SECONDS` | `45` | Lower bound of the random gap between checks. |
| `MAX_CHECK_INTERVAL_SECONDS` | `120` | Upper bound. |
| `ACTIVE_HOURS_START` / `_END` | blank | Optional day/night pacing, in the **venue's** timezone. Blank = one cadence 24/7. |
| `OFFPEAK_MIN/MAX_CHECK_INTERVAL_SECONDS` | `300`/`900` | Cadence outside the active window, when one is set. |
| `NOTIFY_CHANNELS` | `telegram` | Channels to use, e.g. `telegram,email`. Auto-detects when blank. |
| `TELEGRAM_BOT_TOKEN` | — | From @BotFather. |
| `TELEGRAM_CHAT_ID` | — | From `--telegram-setup`. |
| `NOTIFY_EMAIL_TO` | — | Recipient(s), comma-separated (email channel only). |
| `STOP_AFTER_FIRST_NOTIFICATION` | `false` | Set `true` to stop the monitor once it alerts you. |
| `BOOKABLE_SLOT_TYPES` | `book` | Set `book,request` to also hear about request-only slots. |
| `USE_PLAYWRIGHT_FALLBACK` | `false` | Use headless Chromium when the API fails. |
| `SEVENROOMS_VENUE_SLUG` | `lagueritecannes` | From `/reservations/<slug>`. |
| `VENUE_TIMEZONE` | `Europe/Paris` | Used for display. |
| `ERROR_NOTIFICATION_AFTER_FAILURES` | `5` | Consecutive network failures before emailing you. |
| `ERROR_NOTIFICATION_COOLDOWN_SECONDS` | `3600` | Minimum gap between error emails. |
| `HEARTBEAT_HOURS` | `24` | Periodic "still watching" message. `0` disables. |
| `STATE_FILE` | `state/state.json` | Where dedup state is kept. |
| `LOG_FILE` | `logs/monitor.log` | Set to `none` to log to stdout only. |
| `PORT` / `HEALTH_PORT` | unset | If set, serves a JSON status page. |

The interval floor is 30 s and is enforced — the monitor refuses to start below it.

### A note on day/night pacing

It is tempting to poll slowly overnight. For La Guérite that is a mistake, and the
day/night feature is switched off by default because of it. Mapping the venue's major
source markets against Cannes local time, the quietest hour is 01:00 Paris — and even
then it is 19:00 in New York and 16:00 in Los Angeles, exactly when an American guest
would be reorganising their trip. There is no dead hour for an internationally
booked restaurant. Only enable `ACTIVE_HOURS_*` for a venue with a genuinely local
clientele.

---

## The booking link in alerts

Each alert links to the booking page with the date, party size and time already
filled in:

```
https://www.sevenrooms.com/reservations/lagueritecannes?default_date=2026-09-03&default_party_size=6&default_time=14:30
```

Tapping it and pressing **Search** goes straight to `Thu, 3 Sep · 6 Guests · 14:30`.

Two things worth knowing, both established by testing the live widget:

- **The date must be ISO (`2026-09-03`).** The American `09-03-2026` form is silently
  ignored and the calendar stays on the current month — which looks like the link
  "not working".
- **There is no one-click-to-checkout URL.** The widget is a single-page app: choosing
  a time opens a modal without changing the URL, so no link can skip the Search press.
  One click is the floor.

## Duplicate suppression

State lives in `state/state.json` and survives restarts.

| Situation | Behaviour |
|---|---|
| 14:30 appears | Alert. |
| 14:30 still there next check | Silence. |
| 15:00 also appears | Alert about **15:00 only**. |
| 14:30 disappears | Silence — and it is forgotten. |
| 14:30 comes back later | Alert again. |
| Notification send fails | Nothing is remembered, so it retries next check. |
| You change `DATE` / `PARTY_SIZE` / window | Memory clears automatically. |

---

## Logging

One line per check, to stdout and to `logs/monitor.log`:

```
2026-08-22 13:11:48 — Checked successfully — No availability (9 time(s) in window are request-only, not bookable) [sevenrooms-api]
2026-08-22 13:12:18 — AVAILABLE: 2:30 PM, 3:00 PM — Notification sent (sent via email)
2026-08-22 13:12:57 — Check failed (page did NOT load, attempt streak 1) — connection error (ConnectionError)
```

---

## How do I know it's working?

Silence is ambiguous — it could mean "watching, nothing yet" or "the process died".
Three ways to tell them apart:

**1. Ask it.**
```bash
python -m laguerite.monitor --status
```
```
Last check   : 2026-08-23T15:43:24 (0 min ago)
Checks run   : 10
Alerts sent  : 0
Watching for : 2026-09-03, 6 guests, 14:00-16:00

ALIVE — the monitor is running and checking.
```
It prints `STALE` and exits non-zero if the last check is older than three times
your maximum interval.

**2. Wait for the heartbeat.** Every `HEARTBEAT_HOURS` (default 24) it sends a
"still watching" message with its check count and last result. If a day passes with
no heartbeat and no alert, something is wrong.

**3. Watch the log.** One line per check:
```bash
Get-Content logs\monitor.log -Wait -Encoding UTF8    # PowerShell
tail -f logs/monitor.log                             # macOS / Linux
```

## Deploying so it runs while your laptop sleeps

The monitor is a background worker — it needs no inbound HTTP. Pick one host.

> **Do not commit `.env`.** It is already in `.gitignore`. Set secrets in the host's
> dashboard / CLI instead.

```bash
git init
git add .
git commit -m "La Guerite reservation monitor"
```

### Railway (easiest)

1. Push the repo to GitHub.
2. [railway.app](https://railway.app) → **New Project → Deploy from GitHub repo**.
3. **Variables** → add `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
   `NOTIFY_CHANNELS=telegram`, and any settings you want to override.
4. Railway reads `railway.json` and runs `python -m laguerite.monitor`.

Roughly $5/month of usage credit covers this comfortably. Note Railway's filesystem is
ephemeral: a redeploy clears `state.json`, which at worst means one repeated alert.
Attach a volume mounted at `/app/state` if you care.

### Render

1. Push to GitHub → [render.com](https://render.com) → **New → Blueprint**, point it
   at the repo. `render.yaml` defines a worker plus a 1 GB disk for state.
2. Add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in the dashboard (they are marked
   `sync: false` so they are never stored in git).

Background workers need a paid instance type; Render's free tier does not offer them.

### Fly.io

```bash
fly launch --no-deploy --copy-config
fly volumes create laguerite_state --size 1 --region cdg
fly secrets set TELEGRAM_BOT_TOKEN=123456:ABC-xxx TELEGRAM_CHAT_ID=987654321
fly deploy
```

`fly.toml` pins the app to Paris (`cdg`) and mounts a volume at `/data` so state
survives restarts.

### Docker anywhere

```bash
docker build -t laguerite-monitor .
docker run -d --name laguerite --restart unless-stopped \
  --env-file .env \
  -v laguerite_state:/app/state \
  laguerite-monitor
```

The image is based on Playwright's Python image, so the browser fallback works out of
the box — set `USE_PLAYWRIGHT_FALLBACK=true` to enable it.

---

## Checking logs

| Host | Command |
|---|---|
| Local | `Get-Content logs\monitor.log -Wait -Encoding UTF8` (PowerShell) or `tail -f logs/monitor.log` |
| Railway | Dashboard → service → **Deployments → View Logs**, or `railway logs` |
| Render | Dashboard → service → **Logs** |
| Fly.io | `fly logs` |
| Docker | `docker logs -f laguerite` |

If you set `PORT`, `GET /` returns the monitor's current state as JSON.

---

## Stopping it once you have the table

- **Local:** `Ctrl+C` (it shuts down cleanly and saves state).
- **Railway:** service → **Settings → Remove**, or pause the deployment.
- **Render:** service → **Settings → Suspend**.
- **Fly.io:** `fly scale count 0` (or `fly apps destroy laguerite-monitor`).
- **Docker:** `docker stop laguerite && docker rm laguerite`.

Or set `STOP_AFTER_FIRST_NOTIFICATION=true` up front and it stops itself after the
first alert. That decision is remembered in `state.json`; `--reset-state` restarts the
hunt.

---

## Being a good citizen

- One lightweight request per check, at a randomised 1–5 minute interval.
- Exponential backoff on failures; HTTP 429 triggers a minimum 60 s cool-off.
- Only public, unauthenticated endpoints that the booking page itself uses.
- No CAPTCHA, bot-protection, or login bypassing of any kind.

Please leave the interval at 60 s or higher. Hammering the endpoint risks getting your
IP blocked, which would cost you the reservation rather than win it.
