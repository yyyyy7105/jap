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
uv run jap r watch                         # check repeatedly; NEW slots are highlighted
```

On each run, slots you haven't seen before are marked **NEW**; `--new-only` hides the rest.
Seen slots are kept in `.state/seen_slots.json`. Use
`-c` / `-s` to point at other watchlist / settings files.

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
  For shops that split seating (e.g. counter / Private room), Info shows which seating each open slot is for, in the email and the log. Nothing is filtered by it; the extra checks only run when something is open.
- **Tabelog** `seat_only = true`, `plan_id = "..."`.
- **Pocket Concierge**
  - `seating = ["counter"]`: only these seating types. Choose from `counter`, `private counter`, `private room`,
    `table`, `takeout`, `unspecified` (the restaurant didn't say). Leave it out for all.
  - `include_waitlist = true`: also report waitlist-only seatings (marked "waitlist").
  - Only seatings whose allowed party size fits `party_size` are reported. Members-only "secret seats" aren't checked.
  - Info shows one line per time: one course looks like `dinner · Omakase · ¥29,700 · counter`, several like
    `dinner · counter, private room · 4 courses from ¥22,000`.

### Email: new-slot alerts + daily digest

Each recipient gets two kinds of email, only about the restaurants bound to them:

1. **Alert, right away**: whenever a check finds a slot they haven't been emailed about, they get an email
   with just the new slot(s). The same slot is never alerted twice while it stays open.
2. **Daily digest** at `send_at` (default `00:00`, this computer's clock), listing **all** open slots:
   - **NEW**: slots that weren't in the previous digest (including ones already alerted that day). Listed first and highlighted.
   - **Still open**: slots from the previous digest that are still bookable.
   - When the scan ran and which date/time ranges were scanned. Restaurants whose check failed are named too.

A slot that gets booked is forgotten, so if it reopens it's alerted again.

The account lives in `settings.toml` under `[email]`. Who hears about which restaurant is set per
restaurant with `notify` in `watchlist.toml`:

```toml
# settings.toml
[email]
send_at = "00:00"                       # daily digest time, or "off" for new-slot alerts only
send_empty = true                       # also email "nothing open" days (false = skip them)
username = "you@gmail.com"
password = "your-app-password"
default_to = ["you@gmail.com"]          # for restaurants with no / empty `notify`
[email.groups]
me = ["you@gmail.com"]
family = ["you@gmail.com", "partner@example.com"]
```

```bash
uv run jap r watch --email              # watch; alerts as slots appear, digest at send_at every day
uv run jap r digest                     # check now and send everyone their digest immediately
uv run jap r test-email                 # test message to everyone any restaurant notifies
uv run jap r test-email --to family     # ...or just a group / address
```

Run it in the background on Windows (keeps going after the terminal closes; stops at logoff or restart):

```powershell
.\start-watch.ps1     # starts `jap r watch --email` hidden; PID in .state\watch.pid, output in .state\watch.log
.\stop-watch.ps1      # stops it (and the python process under uv) and removes the PID file
```

- `watch` wakes up for `send_at`, runs a fresh check, then sends. The countdown shows the next digest time.
  If the computer was asleep at `send_at`, the digest goes out on the first check after it wakes.
- First run (or after clearing `.state/`): everything currently open is new, so each person gets one alert
  with all of it straight away. Their first digest is at the next `send_at`; `jap r digest` sends one now.
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
