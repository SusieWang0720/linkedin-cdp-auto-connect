#!/usr/bin/env python3
"""send_daily.py — Send the next N LinkedIn connect invites with personalized notes.

Reads queue.jsonl produced by find_leads.py, picks the next N items not yet
attempted (sent.jsonl tracks attempts), and submits each via:
  goto https://www.linkedin.com/preload/custom-invite/?vanityName=<vanity>
  click "Add a note" -> fill -> click Send.

Usage:
  python send_daily.py \
      --out-dir ~/.linkedin-outreach \
      --daily 10 \
      --cdp http://127.0.0.1:9222 \
      --min-delay 35 --max-delay 60

Designed to be safe to invoke multiple times and from launchd:
- Skips items already in sent.jsonl
- Hard cap: never sends more than --daily per calendar day in the user's locale.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import random
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PWTimeout


def already_attempted(sent_path: Path) -> set[str]:
    out: set[str] = set()
    if not sent_path.exists():
        return out
    for line in sent_path.read_text().splitlines():
        try:
            r = json.loads(line)
            if r.get("vanity"):
                out.append(r["vanity"]) if False else out.add(r["vanity"])
        except Exception:
            pass
    return out


def sent_today_count(sent_path: Path) -> int:
    if not sent_path.exists():
        return 0
    today = datetime.date.today().isoformat()
    n = 0
    for line in sent_path.read_text().splitlines():
        try:
            r = json.loads(line)
            ts = r.get("ts", "")
            if ts.startswith(today) and r.get("result", {}).get("status") == "sent":
                n += 1
        except Exception:
            pass
    return n


async def find_invite_dialog(page, timeout_ms=12000) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout_ms / 1000
    while asyncio.get_event_loop().time() < deadline:
        ok = await page.evaluate(
            """
            () => {
              const isVis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
              const dialogs = Array.from(document.querySelectorAll('div[role="dialog"]')).filter(isVis);
              return dialogs.some(d => /add a note to your invitation|how do you know|invitation limit|you have unlimited notes/i.test(d.innerText || ''));
            }
            """
        )
        if ok:
            return True
        await asyncio.sleep(0.4)
    return False


async def send_one(page, target: dict) -> dict:
    vanity = target.get("vanity")
    if not vanity:
        return {"status": "skip", "reason": "no_vanity"}
    invite_url = f"https://www.linkedin.com/preload/custom-invite/?vanityName={vanity}"
    await page.goto(invite_url, wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(5)

    if not await find_invite_dialog(page):
        body_text = (await page.evaluate("() => document.body.innerText || ''")).lower()
        if "pending" in body_text and "invitation" in body_text:
            return {"status": "skip", "reason": "already_pending"}
        return {"status": "skip", "reason": "no_invite_dialog"}

    dialog_text = (await page.evaluate(
        '''() => {
          const isVis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
          const d = Array.from(document.querySelectorAll('div[role="dialog"]')).filter(isVis).find(x => /add a note to your invitation|how do you know|invitation limit/i.test(x.innerText||''));
          return d ? (d.innerText||'').toLowerCase() : '';
        }'''
    )) or ""

    if "how do you know" in dialog_text:
        await page.keyboard.press("Escape")
        return {"status": "skip", "reason": "requires_email"}
    if "invitation limit" in dialog_text:
        await page.keyboard.press("Escape")
        return {"status": "skip", "reason": "weekly_limit"}

    note_added = False
    try:
        add_note = page.locator('div[role="dialog"] button:has-text("Add a note")').first
        await add_note.click(timeout=5000)
        await asyncio.sleep(1.2)
        ta = page.locator('div[role="dialog"] textarea').first
        await ta.fill(target.get("note", "")[:200], timeout=5000)
        note_added = True
        await asyncio.sleep(0.6)
    except Exception as e:
        return {"status": "skip", "reason": f"note_failed: {type(e).__name__}"}

    send_btn = None
    for sel in [
        'div[role="dialog"] button[aria-label="Send invitation"]',
        'div[role="dialog"] button[aria-label="Send now"]',
        'div[role="dialog"] button:has-text("Send invitation")',
        'div[role="dialog"] button:has-text("Send")',
    ]:
        loc = page.locator(sel).first
        if await loc.count() > 0:
            send_btn = loc
            break
    if not send_btn:
        return {"status": "skip", "reason": "send_button_not_found"}
    try:
        await send_btn.click(timeout=5000)
    except Exception as e:
        return {"status": "skip", "reason": f"send_click_failed: {type(e).__name__}"}
    await asyncio.sleep(2.5)
    return {"status": "sent", "note_added": note_added}


async def main_async(args):
    out_dir = Path(args.out_dir).expanduser()
    queue_path = out_dir / "queue.jsonl"
    sent_path = out_dir / "sent.jsonl"
    log_dir = out_dir / "runs"
    log_dir.mkdir(parents=True, exist_ok=True)

    if not queue_path.exists():
        print("queue.jsonl not found — run find_leads.py first", flush=True)
        return

    already = already_attempted(sent_path)
    todays_count = sent_today_count(sent_path)
    remaining_today = max(args.daily - todays_count, 0)
    if remaining_today == 0:
        print(f"daily cap reached ({args.daily}) — already sent {todays_count} today")
        return

    targets: list[dict] = []
    for line in queue_path.read_text().splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if item.get("vanity") in already:
            continue
        targets.append(item)
        if len(targets) >= remaining_today:
            break
    if not targets:
        print("nothing to send (queue exhausted or all attempted)")
        return

    log_path = log_dir / f"run_{datetime.datetime.now():%Y%m%d_%H%M%S}.json"
    log: list[dict] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(args.cdp)
        ctx = browser.contexts[0]
        page = next((p for p in ctx.pages if "linkedin.com" in (p.url or "")), None)
        if not page:
            page = await ctx.new_page()
            await page.goto("https://www.linkedin.com/feed/")
        await page.bring_to_front()

        for i, t in enumerate(targets, 1):
            print(f"[{i}/{len(targets)}] {t.get('name')} - {t.get('company')}", flush=True)
            try:
                result = await send_one(page, t)
            except Exception as e:
                result = {"status": "error", "reason": f"{type(e).__name__}: {e}"}
            record = {
                **t,
                "result": result,
                "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            }
            log.append(record)
            log_path.write_text(json.dumps(log, ensure_ascii=False, indent=2))
            with sent_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            print(f"   -> {result}", flush=True)
            if i < len(targets):
                pause = random.randint(args.min_delay, args.max_delay)
                print(f"   sleep {pause}s", flush=True)
                await asyncio.sleep(pause)
        await browser.close()
    print(f"done — log: {log_path}")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out-dir", default="~/.linkedin-outreach")
    p.add_argument("--cdp", default="http://127.0.0.1:9222")
    p.add_argument("--daily", type=int, default=10)
    p.add_argument("--min-delay", type=int, default=35)
    p.add_argument("--max-delay", type=int, default=60)
    args = p.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
