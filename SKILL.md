---
name: linkedin-cdp-auto-connect
description: Fully automated LinkedIn outreach starting from just a list of company names. Discovers each company's likely key person on LinkedIn, generates a personalized connect note, and sends ≤10 invites per day on autopilot via Playwright over Chrome DevTools Protocol (CDP). Use when the user provides any file (CSV / XLSX / TXT) whose first column is company names and asks for daily LinkedIn outreach. Bypasses LinkedIn's anti-automation by using real CDP-driven user events on a dedicated Chrome launched with --remote-debugging-port and a dedicated --user-data-dir. The user logs into LinkedIn once in that profile; afterwards the daily run is fully unattended. Each invite uses a directly-routed /preload/custom-invite/?vanityName=<vanity> URL — the in-page More→Connect menu item is a link, not a button, and going to its href is what reliably triggers the invite modal. Hard cap defaults to 10 per calendar day; random 35–60s spacing.
description_zh: LinkedIn 全自动批量建联（CDP）
description_en: LinkedIn auto-connect via CDP
disable: false
agent_created: true
---

# linkedin-cdp-auto-connect

End-to-end LinkedIn outreach pipeline:
**company list (file)** → discover lead profiles → daily auto-send 10 invites with personalized note.

## When to use
- User provides any file whose first column is **company names** (CSV / XLSX / TXT) and wants daily LinkedIn outreach.
- User asks to "automate LinkedIn", "send 10 connect a day", "find key persons for these companies", or similar.
- User has explicitly authorized fully-automated sending (no per-target click confirmation). Without authorization, fall back to half-automated mode (run `find_leads.py` only, then stop before send).
- macOS with Google Chrome installed.

Do NOT use this skill for:
- Mass spam, scraping at scale, or unsolicited bulk outreach beyond a low daily cap.
- Sending to anyone the user has not provided in their input list (directly or through company → discovered person).

## Inputs
A single file. Examples that all work:

| File | Format | First column |
|------|--------|--------------|
| `companies.csv` | CSV | `Aylo`, `MindGeek`, ... |
| `companies.txt` | TXT (one per line) | `Aylo` |
| `targets.xlsx` | XLSX (header optional) | first column = company name |

A `templates/companies.csv` sample is included.

## Prerequisites (MUST install before first run)

This skill is **self-contained** — it does NOT depend on any other WorkBuddy
skill/plugin (not `agent-browser`, not `playwright-cli`, not the MCP browser
skills). Those drive a separate browser; this skill connects directly to the
user's own logged-in Chrome via CDP. The real dependencies are system-level:

| Dependency | Why | Install |
|---|---|---|
| **Google Chrome** (desktop) | the browser we drive over CDP | install from google.com/chrome |
| **Python 3** | runs the scripts | macOS built-in, or Anaconda |
| **playwright** (Python pkg) | CDP automation library | `pip install playwright` |
| **openpyxl** (Python pkg) | read `.xlsx` company lists | `pip install openpyxl` |

One-liner:
```bash
pip install playwright openpyxl
```

> **Do NOT run `playwright install`.** This skill attaches to an existing Chrome
> over CDP (`connect_over_cdp`); it does not use Playwright's bundled Chromium,
> so the browser-download step is unnecessary.
>
> Having WorkBuddy's browser skills installed does NOT mean the Python
> `playwright` package is present — that is a separate environment. Always run
> the `pip install` above.
>
> macOS only. launchd scheduling (step 4) needs nothing extra.

## Pipeline (1 → 2 → 3)

### 1. Launch the dedicated Chrome (one shell command, once per session)

```bash
bash <skill_dir>/scripts/start_chrome.sh
```

This opens Chrome with `--remote-debugging-port=9222` against `~/.linkedin-chrome` (a dedicated user-data-dir). On the **first** launch, the user must manually log into LinkedIn in that window. From then on the profile keeps the session, so subsequent runs skip login.

> Why a dedicated user-data-dir? Chrome refuses `--remote-debugging-port` on the default profile and the port silently 404s.

Verify:
```bash
curl -s http://127.0.0.1:9222/json/version
```
Should return JSON with `Browser`, `Protocol-Version`, `webSocketDebuggerUrl`.

### 2. Discover key-person LinkedIn profiles for every company

```bash
python <skill_dir>/scripts/find_leads.py \
    --input /path/to/companies.csv \
    --out-dir ~/.linkedin-outreach
```

For each company:
1. Opens `https://www.linkedin.com/search/results/people/?keywords=<Company>`.
2. Scrapes the visible result list for `/in/<vanity>` links and surrounding text.
3. Scores each candidate by hits on target titles (`CEO/Founder/CTO/VP/Head of Partnerships/...`) and presence of company name, plus how high they appear.
4. Writes:
   - `~/.linkedin-outreach/leads.json` — full ranked candidate pool.
   - `~/.linkedin-outreach/queue.jsonl` — one line per company with the **top** candidate `{company, name, title, url, vanity, note}` ready to send.

Re-running is idempotent: companies already in `queue.jsonl` are skipped.

You can edit `queue.jsonl` by hand to swap in a better candidate (or to delete a row), then continue.

### 3. Send the daily batch (auto-stops at the daily cap)

```bash
python <skill_dir>/scripts/send_daily.py \
    --out-dir ~/.linkedin-outreach \
    --daily 10
```

What it does:
1. Connects to Chrome over CDP.
2. Loads `queue.jsonl`, skips items already in `sent.jsonl`, and computes how many already went out today.
3. Sends up to (`--daily` − today's count) invites:
   - `goto https://www.linkedin.com/preload/custom-invite/?vanityName=<vanity>` — directly opens the invite modal.
   - Wait for a *visible* `div[role="dialog"]` whose text contains `Add a note to your invitation` (or `How do you know` / `invitation limit`).
   - Click `Add a note` → fill the textarea with the note → click `Send invitation` (fallback `Send`).
4. Appends each result to `sent.jsonl` and per-run JSON to `runs/run_<ts>.json`.
5. Random 35–60s sleep between invites.

Status values you'll see:
- `sent` + `note_added: true` → invite was actually delivered with the note.
- `already_pending` → already invited; safely skipped.
- `requires_email` / `weekly_limit` / `no_invite_dialog` → blocked or rate-limited; not counted as sent.

### Optional 4. Schedule it daily with launchd

A template is provided at `templates/com.linkedin.outreach.daily.plist`. Replace placeholders and install:

```bash
SKILL_DIR=<skill_dir>
RUN=$SKILL_DIR/scripts/run_daily.sh
sed \
  -e "s|__RUN_DAILY__|$RUN|g" \
  -e "s|__HOUR__|10|g" \
  -e "s|__MINUTE__|0|g" \
  $SKILL_DIR/templates/com.linkedin.outreach.daily.plist \
  > ~/Library/LaunchAgents/com.linkedin.outreach.daily.plist
launchctl load ~/Library/LaunchAgents/com.linkedin.outreach.daily.plist
```

`run_daily.sh`:
- Ensures the dedicated Chrome is up (calls `start_chrome.sh`; idempotent).
- Calls `send_daily.py` with the configured daily cap.
- Logs to `~/.linkedin-outreach/runs/launchd_<ts>.log`.

To disable: `launchctl unload ~/Library/LaunchAgents/com.linkedin.outreach.daily.plist`.

> Note: launchd cannot wake macOS from full sleep just to run this. Either keep the Mac awake / on charger, or run the daily script manually.

## File layout under `~/.linkedin-outreach`

```
~/.linkedin-outreach/
├── leads.json        # ranked candidate pool from find_leads.py
├── queue.jsonl       # next-up: 1 line per company (editable)
├── sent.jsonl        # append-only: 1 line per attempted invite
└── runs/             # per-invocation JSON logs and launchd shell logs
```

## Pitfalls
- **Default user-data-dir blocks remote debugging.** Chrome silently logs `DevTools remote debugging requires a non-default data directory` and `:9222` answers HTTP 404. Always use a dedicated dir (`start_chrome.sh` handles this).
- **AppleScript-driven `.click()` does NOT work.** LinkedIn requires user-activation events; AppleScript-injected JS clicks silently fail to open the invite modal (you only see the hidden video-caption settings dialog). CDP via Playwright is required.
- **Direct Connect button is rare.** For 1st-degree connections it's `Message`, for many 3rd-degree it's `Follow`. Connect lives inside the "More" dropdown as `<a role="menuitem" href="/preload/custom-invite/?vanityName=…">`. **Skip clicking the menu — `goto` the href.**
- **Hidden dialogs.** LinkedIn always renders `<div role="dialog" aria-label="Modal Window">` (video player error) and `<div aria-label="Caption Settings Dialog">` even when not displayed. Filter dialogs by `getBoundingClientRect()` width/height > 0 before scanning innerText.
- **Daily/weekly limits.** LinkedIn caps invites at ~100/week for free accounts. Keep `--daily ≤ 10`. Random 35–60s spacing is the default.
- **"How do you know X" / email gate.** If LinkedIn requires an email or relationship type, dismiss and skip — never guess.
- **Note > 200 chars** silently disables the Send button. Free-account note limit is 200 (Premium 300). `find_leads.py` truncates to 200.
- **Managed Python may fail to load native wheels** on macOS due to code-signing Team ID mismatch (psutil, greenlet, etc.). Use system Anaconda (`/opt/anaconda3/bin/python3`) or a fresh system venv.
- **Discovery is best-effort.** Some companies (especially low-profile or sensitive industries) won't yield a confident match — `find_leads.py` writes only candidates with non-zero score. Always inspect `queue.jsonl` before launching `send_daily.py` for the first time on a new list.
- **Send rate-limit errors don't block the queue.** When LinkedIn returns `weekly_limit`, the runner skips and continues; the rate-limited targets stay queueable but are marked attempted in `sent.jsonl`. Re-add them to `queue.jsonl` after the limit resets if needed.

## Verification
- After `find_leads.py`: `wc -l ~/.linkedin-outreach/queue.jsonl` should roughly match the number of companies with a confident match.
- After `send_daily.py`: a fresh `runs/run_*.json` should contain `status: "sent"` and `note_added: true` for the day's invites.
- LinkedIn → My Network → Sent invitations should list the new invites with the personalized note attached.

## Files in this skill
- `scripts/start_chrome.sh` — launch dedicated Chrome with CDP enabled.
- `scripts/find_leads.py` — discover key-person profiles per company.
- `scripts/send_daily.py` — send the day's invite batch through CDP.
- `scripts/run_daily.sh` — launchd-friendly wrapper around the two above.
- `scripts/cdp_auto_connect.py` — original hand-tuned script for a fixed 10-target list (kept as reference).
- `templates/companies.csv` — input file template.
- `templates/com.linkedin.outreach.daily.plist` — launchd schedule template.
