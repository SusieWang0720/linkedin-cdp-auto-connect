# linkedin-cdp-auto-connect

Fully automated LinkedIn outreach starting from just a list of company names.

Discovers each company's likely key person on LinkedIn, generates a personalized
connect note, and sends ≤10 invites per day on autopilot via [Playwright](https://playwright.dev/python/)
over the Chrome DevTools Protocol (CDP).

Originally packaged as a [WorkBuddy](https://workbuddy.dev) skill, but the scripts
work standalone on macOS.

## Why CDP?

LinkedIn blocks AppleScript-injected JS clicks (no user-activation), and the
in-page More → Connect entry is an `<a>` whose `href` is the only path that
reliably opens the invite modal. Driving real Chrome over CDP makes Playwright's
clicks count as user events, so the flow goes through.

## Inputs

A single file. Examples that all work:

| File              | Format               | First column     |
| ----------------- | -------------------- | ---------------- |
| `companies.csv`   | CSV                  | `Aylo`, `MindGeek`, ... |
| `companies.txt`   | TXT (one per line)   | `Aylo`           |
| `targets.xlsx`    | XLSX (header optional) | first column = company name |

A `templates/companies.csv` sample is included.

## Pipeline

```
companies file ──► find_leads.py ──► queue.jsonl
                                       │
                                       ▼
                                 send_daily.py ──► sent.jsonl + runs/run_*.json
                                       │
                                       ▼
                                  (LinkedIn invite sent with personalized note)
```

### 1. Launch the dedicated Chrome (one shell command, once per session)

```bash
bash scripts/start_chrome.sh
```

This opens Chrome with `--remote-debugging-port=9222` against `~/.linkedin-chrome`
(a dedicated `--user-data-dir`). On the **first** launch you must manually log
into LinkedIn in that window. After that the profile keeps the session, so
subsequent runs skip login.

> Why a dedicated user-data-dir? Chrome refuses `--remote-debugging-port` on
> the default profile and the port silently 404s.

Verify:

```bash
curl -s http://127.0.0.1:9222/json/version
```

Should return JSON with `Browser`, `Protocol-Version`, `webSocketDebuggerUrl`.

### 2. Discover key-person LinkedIn profiles for every company

```bash
python scripts/find_leads.py \
    --input /path/to/companies.csv \
    --out-dir ~/.linkedin-outreach
```

For each company:

1. Opens `https://www.linkedin.com/search/results/people/?keywords=<Company>`.
2. Scrapes the visible result list for `/in/<vanity>` links and surrounding text.
3. Scores each candidate by hits on target titles (`CEO/Founder/CTO/VP/Head of Partnerships/...`)
   and presence of company name, plus how high they appear.
4. Writes:
   - `~/.linkedin-outreach/leads.json` — full ranked candidate pool.
   - `~/.linkedin-outreach/queue.jsonl` — one line per company with the **top**
     candidate `{company, name, title, url, vanity, note}` ready to send.

Re-running is idempotent: companies already in `queue.jsonl` are skipped.

You can edit `queue.jsonl` by hand to swap in a better candidate (or to delete
a row), then continue.

### 3. Send the daily batch (auto-stops at the daily cap)

```bash
python scripts/send_daily.py \
    --out-dir ~/.linkedin-outreach \
    --daily 10
```

What it does:

1. Connects to Chrome over CDP.
2. Loads `queue.jsonl`, skips items already in `sent.jsonl`, and computes how
   many already went out today.
3. Sends up to `(--daily − today's count)` invites:
   - `goto https://www.linkedin.com/preload/custom-invite/?vanityName=<vanity>` —
     directly opens the invite modal.
   - Wait for a *visible* `div[role="dialog"]` whose text contains
     `Add a note to your invitation` (or `How do you know` / `invitation limit`).
   - Click `Add a note` → fill the textarea with the note → click
     `Send invitation` (fallback `Send`).
4. Appends each result to `sent.jsonl` and per-run JSON to `runs/run_<ts>.json`.
5. Random 35–60s sleep between invites.

Status values you'll see:

- `sent` + `note_added: true` — invite was actually delivered with the note.
- `already_pending` — already invited; safely skipped.
- `requires_email` / `weekly_limit` / `no_invite_dialog` — blocked or
  rate-limited; not counted as sent.

### 4. (Optional) Schedule it daily with launchd

A template is provided at `templates/com.linkedin.outreach.daily.plist`.
Replace placeholders and install:

```bash
SKILL_DIR=$(pwd)
RUN=$SKILL_DIR/scripts/run_daily.sh
sed \
  -e "s|__RUN_DAILY__|$RUN|g" \
  -e "s|__HOUR__|22|g" \
  -e "s|__MINUTE__|0|g" \
  $SKILL_DIR/templates/com.linkedin.outreach.daily.plist \
  > ~/Library/LaunchAgents/com.linkedin.outreach.daily.plist
launchctl load ~/Library/LaunchAgents/com.linkedin.outreach.daily.plist
```

`scripts/run_daily.sh`:

- Ensures the dedicated Chrome is up (calls `start_chrome.sh`; idempotent).
- Calls `send_daily.py` with the configured daily cap.
- Logs to `~/.linkedin-outreach/runs/launchd_<ts>.log`.

To disable:

```bash
launchctl unload ~/Library/LaunchAgents/com.linkedin.outreach.daily.plist
```

> Note: launchd cannot wake macOS from full sleep just to run this. Either keep
> the Mac awake / on charger, or run the daily script manually.

## File layout under `~/.linkedin-outreach`

```
~/.linkedin-outreach/
├── leads.json        # ranked candidate pool from find_leads.py
├── queue.jsonl       # next-up: 1 line per company (editable)
├── sent.jsonl        # append-only: 1 line per attempted invite
└── runs/             # per-invocation JSON logs and launchd shell logs
```

## Repository layout

```
linkedin-cdp-auto-connect/
├── README.md
├── SKILL.md                                # WorkBuddy skill definition
├── scripts/
│   ├── start_chrome.sh                     # launch dedicated Chrome with CDP enabled
│   ├── find_leads.py                       # discover key-person profiles per company
│   ├── send_daily.py                       # send the day's invite batch through CDP
│   ├── run_daily.sh                        # launchd-friendly wrapper
│   └── cdp_auto_connect.py                 # one-off fixed-list reference script
└── templates/
    ├── companies.csv
    └── com.linkedin.outreach.daily.plist
```

## Pitfalls

- **Default user-data-dir blocks remote debugging.** Chrome silently logs
  `DevTools remote debugging requires a non-default data directory` and `:9222`
  answers HTTP 404. Always use a dedicated dir (`start_chrome.sh` handles this).
- **AppleScript-driven `.click()` does NOT work.** LinkedIn requires
  user-activation events; AppleScript-injected JS clicks silently fail to open
  the invite modal (you only see the hidden video-caption settings dialog).
  CDP via Playwright is required.
- **Direct Connect button is rare.** For 1st-degree connections it's `Message`,
  for many 3rd-degree it's `Follow`. Connect lives inside the "More" dropdown
  as `<a role="menuitem" href="/preload/custom-invite/?vanityName=…">`.
  **Skip clicking the menu — `goto` the href.**
- **Hidden dialogs.** LinkedIn always renders
  `<div role="dialog" aria-label="Modal Window">` (video player error) and
  `<div aria-label="Caption Settings Dialog">` even when not displayed.
  Filter dialogs by `getBoundingClientRect()` width/height > 0 before
  scanning innerText.
- **Daily/weekly limits.** LinkedIn caps invites at ~100/week for free
  accounts. Keep `--daily ≤ 10`. Random 35–60s spacing is the default.
- **"How do you know X" / email gate.** If LinkedIn requires an email or
  relationship type, dismiss and skip — never guess.
- **Note > 200 chars** silently disables the Send button. Free-account note
  limit is 200 (Premium 300). `find_leads.py` truncates to 200.
- **Discovery is best-effort.** Some companies (especially low-profile or
  sensitive industries) won't yield a confident match — `find_leads.py`
  writes only candidates with non-zero score. Always inspect `queue.jsonl`
  before launching `send_daily.py` for the first time on a new list.

## Disclaimer

LinkedIn's [User Agreement](https://www.linkedin.com/legal/user-agreement)
forbids automation. This project is provided as a research/educational
example of CDP-driven browser automation. Use it at your own risk; mass
outreach can get your account restricted or banned. The defaults are
intentionally conservative (≤ 10 invites/day, 35–60s random spacing).

## License

MIT.
