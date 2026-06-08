#!/usr/bin/env python3
"""find_leads.py — Discover LinkedIn lead profiles for each company.

Reads a company list (CSV/XLSX/TXT) and uses an already-logged-in Chrome
attached over CDP to search LinkedIn for likely "key person" profiles.

Output:
  <out_dir>/leads.json — full candidate pool (multiple per company)
  <out_dir>/queue.jsonl — flat queue of {company, name, title, url, vanity, note}

Usage:
  python find_leads.py \
      --input /path/to/companies.csv \
      --out-dir ~/.linkedin-outreach \
      --cdp http://127.0.0.1:9222 \
      --target-titles "CEO,Founder,Co-founder,CTO,VP Engineering,Head of Partnerships,Business Development,Cloud,Product"

Notes:
- Requires the user to have a Chrome already running with --remote-debugging-port and --user-data-dir
  containing a logged-in LinkedIn session. See SKILL.md for one-time setup.
- Skips companies that already have entries in queue.jsonl (idempotent).
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path
from typing import Iterable

from playwright.async_api import async_playwright

DEFAULT_TITLES = [
    "CEO", "Founder", "Co-founder", "Co-Founder", "Cofounder",
    "CTO", "Chief Technology", "Chief Product", "CPO",
    "VP Engineering", "VP Product", "VP Partnerships", "Head of Partnerships",
    "Head of Business Development", "Business Development",
    "Cloud", "Infrastructure", "Platform"
]


def read_companies(path: Path) -> list[str]:
    """Return ordered, de-duped list of company names from a CSV/XLSX/TXT file."""
    suffix = path.suffix.lower()
    rows: list[str] = []
    if suffix in (".csv", ".tsv", ".txt"):
        delim = "\t" if suffix == ".tsv" else ","
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            sniff = f.read(2048)
            f.seek(0)
            if "," not in sniff and "\t" not in sniff:
                # plain TXT — one company per line
                for line in f:
                    name = line.strip()
                    if name:
                        rows.append(name)
                return list(dict.fromkeys(rows))
            reader = csv.reader(f, delimiter=delim)
            header = next(reader, None)
            # heuristic: take first non-empty cell of each row as company name
            for r in reader:
                if not r:
                    continue
                cell = r[0].strip()
                if cell:
                    rows.append(cell)
    elif suffix in (".xlsx", ".xlsm"):
        try:
            import openpyxl  # type: ignore
        except ImportError:
            print("openpyxl is required for xlsx input", file=sys.stderr)
            sys.exit(2)
        wb = openpyxl.load_workbook(path, data_only=True)
        ws = wb.active
        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row:
                continue
            cell = row[0]
            if isinstance(cell, str) and cell.strip():
                rows.append(cell.strip())
    else:
        print(f"Unsupported file type: {suffix}", file=sys.stderr)
        sys.exit(2)
    return list(dict.fromkeys(rows))


def make_note(company: str, name: str, title: str) -> str:
    first = name.split()[0]
    note = (
        f"Hi {first}, I came across {company} and your work as {title}. "
        f"I'd like to connect and exchange ideas around platform growth, cloud infrastructure, "
        f"and potential partnership opportunities."
    )
    return note[:200]  # LinkedIn free-account note limit


def vanity_from_url(url: str) -> str | None:
    m = re.search(r"linkedin\.com/in/([^/?#]+)", url)
    return m.group(1) if m else None


async def search_company_people(page, company: str, titles: list[str], top_n: int) -> list[dict]:
    """Return ranked candidates for a company.

    Strategy:
      1) Search 'people' results with the company name as keyword.
      2) Score each based on whether one of the target titles is in the result text.
      3) Return up to top_n profiles.
    """
    keyword = urllib.parse.quote(company)
    url = f"https://www.linkedin.com/search/results/people/?keywords={keyword}&origin=GLOBAL_SEARCH_HEADER"
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(4)

    raw = await page.evaluate(
        """
        () => {
          const seen = new Set();
          const out = [];
          const links = Array.from(document.querySelectorAll('a[href*="/in/"]'));
          for (const a of links) {
            const href = a.href.split('?')[0];
            if (seen.has(href)) continue;
            seen.add(href);
            // climb to the LI / row container to capture name + title text
            let el = a;
            for (let i = 0; i < 6 && el && el.parentElement; i++) {
              if ((el.innerText || '').length > 30) break;
              el = el.parentElement;
            }
            const text = (el ? el.innerText : a.innerText) || '';
            out.push({ href, text: text.replace(/\\s+/g, ' ').trim().slice(0, 400) });
            if (out.length >= 20) break;
          }
          return out;
        }
        """
    )

    candidates: list[dict] = []
    for r in raw:
        if "/company/" in r["href"] or "/school/" in r["href"]:
            continue
        text = r["text"]
        if not text:
            continue
        # cheap scoring
        score = 0
        title_match = ""
        for t in titles:
            if re.search(rf"\b{re.escape(t)}\b", text, re.IGNORECASE):
                score += 5
                if not title_match:
                    title_match = t
        if company.lower() in text.lower():
            score += 3
        # name heuristic: first non-empty line that looks like a name
        first_line = text.split("•")[0].strip().split("\n")[0].strip()
        # Cleanup name (remove trailing degree marker)
        name = re.split(r"\s+•\s+|\s+\d(?:st|nd|rd)|\s+\d+(?:st|nd|rd)\+?", first_line)[0].strip()
        if not name or len(name) > 80:
            continue
        candidates.append({
            "company": company,
            "name": name,
            "title": title_match or first_line,
            "url": r["href"],
            "score": score,
            "raw_text": text,
        })

    candidates.sort(key=lambda x: -x["score"])
    return candidates[:top_n]


async def main_async(args):
    out_dir = Path(args.out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    leads_path = out_dir / "leads.json"
    queue_path = out_dir / "queue.jsonl"

    existing_companies: set[str] = set()
    if queue_path.exists():
        for line in queue_path.read_text().splitlines():
            try:
                existing_companies.add(json.loads(line)["company"])
            except Exception:
                pass

    companies = read_companies(Path(args.input).expanduser())
    print(f"Loaded {len(companies)} companies, {len(existing_companies)} already processed.")

    titles = [t.strip() for t in args.target_titles.split(",") if t.strip()]
    leads: list[dict] = []
    if leads_path.exists():
        try:
            leads = json.loads(leads_path.read_text())
        except Exception:
            leads = []

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(args.cdp)
        ctx = browser.contexts[0]
        page = next((p for p in ctx.pages if "linkedin.com" in (p.url or "")), None)
        if not page:
            page = await ctx.new_page()
        await page.bring_to_front()

        for i, company in enumerate(companies, 1):
            if company in existing_companies:
                print(f"[{i}/{len(companies)}] {company} — skip (already in queue)")
                continue
            print(f"[{i}/{len(companies)}] {company}")
            try:
                cand = await search_company_people(page, company, titles, args.candidates_per_company)
            except Exception as e:
                print(f"   search failed: {type(e).__name__}: {e}")
                continue
            if not cand:
                print("   no candidates found")
                continue
            leads.append({"company": company, "candidates": cand})
            top = cand[0]
            with queue_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "company": company,
                    "name": top["name"],
                    "title": top["title"],
                    "url": top["url"],
                    "vanity": vanity_from_url(top["url"]),
                    "note": make_note(company, top["name"], top["title"] or "your role"),
                }, ensure_ascii=False) + "\n")
            print(f"   top: {top['name']} | {top['title']} | {top['url']}")
            leads_path.write_text(json.dumps(leads, ensure_ascii=False, indent=2))
            await asyncio.sleep(args.delay)
        await browser.close()
    print("done.")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", required=True, help="CSV/XLSX/TXT with company names in first column")
    p.add_argument("--out-dir", default="~/.linkedin-outreach")
    p.add_argument("--cdp", default="http://127.0.0.1:9222")
    p.add_argument("--target-titles", default=",".join(DEFAULT_TITLES))
    p.add_argument("--candidates-per-company", type=int, default=5)
    p.add_argument("--delay", type=float, default=4.0)
    args = p.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
