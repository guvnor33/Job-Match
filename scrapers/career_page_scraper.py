"""
Career-page scraper.
Strategy: httpx + BeautifulSoup first; Playwright fallback for JS-rendered pages.

Run:
    python -m scrapers.career_page_scraper
"""
import asyncio
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent.parent


def _load_config() -> dict:
    return yaml.safe_load((ROOT / "config.yaml").read_text())


JOB_LINK_SIGNALS = re.compile(
    r"(job|career|position|role|opening|vacanc|apply|posting|requisition)",
    re.I,
)


def _extract_job_links(html: str, base_url: str, selector: str | None) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    results = []

    if selector:
        links = soup.select(selector)
    else:
        links = soup.find_all("a", href=True)

    for a in links:
        href = a.get("href", "")
        text = a.get_text(strip=True)
        full_url = urljoin(base_url, href)
        # Only keep links that look job-related
        if JOB_LINK_SIGNALS.search(full_url) or JOB_LINK_SIGNALS.search(text):
            results.append({"url": full_url, "title": text or None})

    return results


def _is_empty_page(html: str) -> bool:
    """Return True if the page has very little visible text — likely JS-rendered."""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(strip=True)
    return len(text) < 200


async def _fetch_with_playwright(url: str) -> str:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(url, wait_until="networkidle", timeout=30000)
        html = await page.content()
        await browser.close()
    return html


def _fetch_html(url: str) -> str:
    """Fetch with httpx; fall back to Playwright if page appears JS-rendered."""
    try:
        resp = httpx.get(url, timeout=15, follow_redirects=True,
                         headers={"User-Agent": "Mozilla/5.0 (JobScrape bot)"})
        html = resp.text
    except Exception as e:
        print(f"  [career] httpx failed for {url}: {e}")
        html = ""

    if _is_empty_page(html):
        print(f"  [career] JS-rendered page detected, using Playwright for {url}")
        html = asyncio.run(_fetch_with_playwright(url))

    return html


def scrape(dry_run: bool = False, stop_event=None, log_fn=None) -> dict:
    from db.models import list_sources, upsert_job, STATUS_NEW, STATUS_EXISTING, STATUS_APPLIED
    from matching.resume_matcher import score_job_if_enabled
    log = log_fn or print

    now = datetime.now().isoformat()
    sources = list_sources(active_only=True)
    log(f"  {len(sources)} active career page source(s)")
    counts = {"new": 0, "scored": 0, "existing": 0, "reviewed": 0, "blacklisted": 0}

    for src in sources:
        if stop_event and stop_event.is_set():
            print("[career] stop requested, halting.")
            break
        company = src["company"]
        base_url = src["url"]
        selector = src["selector"]

        from scrapers._config import is_blacklisted_company
        if is_blacklisted_company(company):
            log(f"[career] skipping {company!r} — blacklisted company")
            counts["blacklisted"] += 1
            continue

        print(f"[career] scraping {company} — {base_url}")

        try:
            html = _fetch_html(base_url)
            links = _extract_job_links(html, base_url, selector)
            print(f"  found {len(links)} job links")

            for link in links:
                url = link["url"]
                title = link["title"]
                label = f"{title or '?'} @ {company}"
                if dry_run:
                    log(f"  [dry] {label}")
                else:
                    job_id, status = upsert_job(
                        source="career_page",
                        company=company,
                        title=title,
                        location=None,
                        url=url,
                        description=None,
                        posted_at=None,
                        fetched_at=now,
                    )
                    if status == STATUS_NEW:
                        score_status = score_job_if_enabled(job_id)
                        if score_status.startswith("scored"):
                            counts["scored"] += 1
                        counts["new"] += 1
                        log(f"  [NEW]      {label} — {score_status}")
                    elif status == STATUS_APPLIED:
                        counts["reviewed"] += 1
                        log(f"  [REVIEWED] {label} — already marked as applied, skipping")
                    else:
                        counts["existing"] += 1
                        log(f"  [EXISTS]   {label} — already in database, skipping")
        except Exception as e:
            print(f"  [career] error scraping {base_url}: {e}")

    return counts


if __name__ == "__main__":
    scrape(dry_run="--dry-run" in sys.argv)
