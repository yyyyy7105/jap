# jap — Japan trip toolkit

A set of components, each a subcommand of `jap`. Managed with [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run jap --help
```

## Component 1: restaurant watcher (`jap restaurants`, alias `jap r`)

Checks reservation availability for the restaurants on your watchlist, but only inside the
dates and times you're watching for each one. All dates and times are Tokyo local time (JST).

| Platform   | How it checks | Status |
|------------|---------------|--------|
| TableCheck | public JSON timetable. If a shop has it switched off, it checks each start time (optionally for a pinned course) | working |
| Tabelog    | the English site's booking-calendar JSON | working |
| Pocket Concierge | the site's GraphQL API: open dates in one request, then the seatings on each open date | working |
| OMAKASE    | real browser using your own login (Playwright) | login and capture work; slot parser not written yet |

### Setup

Two config files (copy the examples and edit):

| File | Holds | Git |
|------|-------|-----|
| `watchlist.toml` | restaurants: `[defaults]` + `[[restaurants]]`, incl. who to email (`notify`) | ignored |
| `settings.toml` | run options (`[settings]`) and the email account / groups (`[email]`) | ignored (has your password) |

```bash
cp watchlist.example.toml watchlist.toml
cp settings.example.toml settings.toml
uv run jap r list                          # restaurants, windows and who gets emailed
uv run jap r check                         # check once
uv run jap r watch                         # check repeatedly in this terminal; NEW slots are highlighted
uv run jap r start                         # the same watcher in the background, with email (PID in .state/watch.pid)
uv run jap r status                        # running? + the end of the log
uv run jap r stop                          # stop the background watcher
```

**Make shortcuts** (`make help` lists them; on Windows without `make`, use `mingw32-make` from MSYS2 or
`choco install make`):

```bash
make start      # = jap r start      make stop / restart / status
make check      # = jap r check      make watch / list / digest / test-email
make ui         # the UI, always on 0.0.0.0 (your whole network), port 8501 (PORT=... to change)
make sync       # uv sync --extra ui  make test
```

On each run, slots you haven't seen before are marked **NEW**; `--new-only` hides the rest.
Seen slots are kept in `.state/seen_slots.json`. Use
`-c` / `-s` to point at other watchlist / settings files.

**One log:** every check, from the CLI (`check`, `digest`, `watch`, `start`) or the UI, is appended to
`.state/watch.log`. It holds the same tables you see in the terminal, plus timestamped lines for
emails sent, config reloads and errors. It rolls over to `watch.log.1` past 5 MB.

**Code layout:** `src/jap/restaurants/` holds the shared core (`watch.py` is the watch loop, logging and
background start/stop; plus `config.py`, `checker.py`, `notify.py`, `providers/`). The two front ends
only call into it: `cli/` (the `jap r ...` commands) and `ui/` (the Streamlit app).

### UI (optional)

A browser UI for both files plus the background watcher:

```bash
uv sync --extra ui
uv run jap r ui                            # opens http://localhost:8501, also reachable from your network (--host 127.0.0.1 = this PC only)
```

- **Watcher**: running or not, Start / Stop (the same as `jap r start` / `jap r stop`), Check now (`jap r check`),
  send a test email, and the shared log.
- **Settings (settings.toml)**: every `[settings]` and `[email]` option, plus recipient groups.
- **Watchlist (watchlist.toml)**: `[defaults]` (party size, notify, windows) and each restaurant: name, enabled,
  URL / platform / id, party size, notify, windows, and the platform's own options. Add and delete restaurants.

Every field shows the file's value, or the built-in default when the file doesn't set it.
Saving checks the file with the same rules the watcher uses and refuses to save if something is wrong.
It keeps the old file in `.state/backups/` and rewrites the whole file with a comment on each setting.
Your own comments and commented-out entries are not kept; use the Enabled box instead of commenting out.
A running watcher notices the saved file within a second, reloads it and checks again (no restart).
If the new file has an error, it keeps the previous settings and says so in the log.

### Watchlist format (`watchlist.toml`)

```toml
[defaults]
party_size = 2
notify = ["me"]                   # default recipients for every restaurant

[[restaurants]]
name = "Wakanui Tokyo"
url  = "https://www.tablecheck.com/en/shops/wakanui-tokyo/reserve"   # or platform + id
party_size = 2
notify = ["family", "friend@example.com"]   # group names and/or addresses
  [[restaurants.windows]]
  start_date = "2026-11-20"
  end_date   = "2026-11-28"
  start_time = "18:00"
  end_time   = "20:30"
  weekdays   = ["fri", "sat"]     # optional
  [[restaurants.windows]]
  date = "2026-11-22"             # single day
  start_time = "11:30"
  end_time   = "13:30"
  [[restaurants.windows]]
  days = 14                       # rolling: today + next 13 days
  start_time = "18:00"
  end_time   = "21:00"
```

Each window uses one of three date forms:

| Form | Covers |
|------|--------|
| `start_date` + `end_date` | that fixed range (`end_date` optional = one day) |
| `date` | one fixed day |
| `days = N` | **rolling**: today (Tokyo date) plus the next N-1 days. Recomputed every check, so a long `watch` keeps moving forward. 1-366. |

All three accept `start_time`, `end_time` and `weekdays`. A restaurant can have several windows; a slot counts
if it fits any of them. Restaurants without windows use `[[defaults.windows]]`.

Per-platform options:
- **TableCheck** `menu_item_id = "..."`: pin a course for shops that require one. This cuts requests a lot. Find IDs with `uv run jap r menus <slug-or-url> --date YYYY-MM-DD`.
  - Every seating type is reported. For shops that split seating (e.g. counter / Private room), Info says which
    seating each open time is for, e.g. `dinner · counter`. That takes a few extra requests, only when something is open.
- **Tabelog** `seat_only = true`, `plan_id = "..."`.
- **Pocket Concierge**
  - Every seating type is reported; Info names it (see below).
  - `include_waitlist = true`: also report waitlist-only seatings (marked "waitlist").
  - Only seatings whose allowed party size fits `party_size` are reported. Members-only "secret seats" aren't checked.
  - Info shows one line per time: one course looks like `dinner · Omakase · ¥29,700 · counter`, several like
    `dinner · counter, private room · 4 courses from ¥22,000`.

### Email: new-slot alerts (+ optional daily digest)

Each recipient gets two kinds of email, only about the restaurants bound to them:

1. **Alert, right away**: whenever a check finds a slot they haven't been emailed about, they get an email
   with just the new slot(s). The same slot is never alerted twice while it stays open.
2. **Daily digest**, off by default. Set `send_at = "HH:MM"` (this computer's clock) to get one every day, listing **all** open slots:
   - **NEW**: slots that weren't in the previous digest (including ones already alerted that day). Listed first and highlighted.
   - **Still open**: slots from the previous digest that are still bookable.
   - When the scan ran and which date/time ranges were scanned. Restaurants whose check failed are named too.

A slot that gets booked is forgotten, so if it reopens it's alerted again.

The account lives in `settings.toml` under `[email]`. Who hears about which restaurant is set per
restaurant with `notify` in `watchlist.toml`:

```toml
# settings.toml
[email]
send_at = "off"                         # default: new-slot alerts only; "00:00" = also a daily digest at midnight
send_empty = true                       # also email "nothing open" days (false = skip them)
username = "you@gmail.com"
password = "your-app-password"
default_to = ["you@gmail.com"]          # for restaurants with no / empty `notify`
[email.groups]
me = ["you@gmail.com"]
family = ["you@gmail.com", "partner@example.com"]
```

```bash
uv run jap r watch --email              # watch; alerts as slots appear (+ digest at send_at, if set)
uv run jap r digest                     # check now and send everyone the full list of open slots, even if the digest is off
uv run jap r test-email                 # test message to everyone any restaurant notifies
uv run jap r test-email --to family     # ...or just a group / address
```

Run it in the background (keeps going after the terminal closes; stops at logoff or restart):
`make start` / `make stop` (= `jap r start` / `jap r stop`; PID in `.state/watch.pid`, log in `.state/watch.log`).

- With `send_at` set, `watch` wakes up for it, runs a fresh check, then sends. The countdown shows the next digest time.
  If the computer was asleep at `send_at`, the digest goes out on the first check after it wakes.
- First run (or after clearing `.state/`): everything currently open is new, so each person gets one alert
  with all of it straight away. With `send_at` set, their first digest is at the next `send_at`; `jap r digest` sends one now.
- `notify` resolution: the restaurant's own list, else `[defaults] notify`, else `default_to`. Unknown group names are a config error.
- Each recipient gets their own email, listing only their restaurants. Recipients never see each other's addresses.
- `enabled = true` makes `watch` email without `--email`; `--no-email` turns it off for one run. `check` never emails.
- Password: `password = "..."` in `settings.toml`, or the env var named by `password_env` (default
  `JAP_SMTP_PASSWORD`, e.g. in a git-ignored `.env`). The env var wins if both are set. Gmail needs an App Password.
- If sending fails, that alert/digest is retried on the next check. Nothing is marked as reported until it's sent.
- Record: `.state/email_digest.json`, per person `last_sent` (digest date), `alerted` (slots emailed about)
  and `digested` (slots in the last digest). Only still-open slots are kept.

### OMAKASE

```bash
uv sync --extra omakase
uv run playwright install chromium
uv run jap r omakase-login            # sign in yourself in the window that opens
uv run jap r omakase-probe <slug>     # saves HTML + JSON to .state/omakase-probe/<slug>/
```

The probe capture is what the slot parser will be built from.

### Notes
- Requests are paced (`request_delay`). Watch mode adds ±15% jitter, and the checker backs off on HTTP 429.
- The tool only reads availability. It never books anything.
