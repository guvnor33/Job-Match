"""
LinkedIn scraper using Playwright with a persistent browser context.

First-time setup (saves your login session):
    python -m scrapers.linkedin_scraper --save-session

Normal run:
    python -m scrapers.linkedin_scraper
"""
import asyncio
import random
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml
from playwright.async_api import async_playwright

ROOT = Path(__file__).parent.parent
SESSION_DIR = ROOT / "linkedin_session"
CONFIG_PATH = ROOT / "config.yaml"


def _load_config() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text())


# ---------------------------------------------------------------------------
# JavaScript-based job extraction (resilient to LinkedIn DOM changes)
# ---------------------------------------------------------------------------

_EXTRACT_JOBS_JS = """
() => {
    const results = [];
    const seen = new Set();

    // Find every link that points to an individual job view page
    document.querySelectorAll('a[href*="/jobs/view/"]').forEach(a => {
        const href = a.href ? a.href.split('?')[0] : '';
        if (!href || seen.has(href)) return;
        seen.add(href);

        // Title: aria-label is most reliable, then text content
        const title = (a.getAttribute('aria-label') || a.innerText || '').trim();
        if (title.length < 3) return;

        // Walk up to the nearest list item / card container
        const card = a.closest('li, article, [data-job-id], [data-occludable-job-id]') || a;

        // Company: try several possible class fragments
        const companyEl =
            card.querySelector('[class*="primary-description"]') ||
            card.querySelector('[class*="company-name"]') ||
            card.querySelector('[class*="subtitle"] span');

        // Location: try several possible class fragments
        const locationEl =
            card.querySelector('[class*="metadata-item"]') ||
            card.querySelector('[class*="workplace-type"]') ||
            card.querySelector('[class*="location"]');

        results.push({
            href,
            title,
            company: companyEl ? companyEl.innerText.trim() : '',
            location: locationEl ? locationEl.innerText.trim() : '',
        });
    });

    return results;
}
"""

# Selector for the result list-item shells (LinkedIn keeps ~25 in the DOM but only
# renders the inner job link for those near the viewport).
_LIST_ITEM_SELECTOR = (
    "li[class*='scaffold-layout__list-item'], "
    "li[data-occludable-job-id], "
    "li.jobs-search-results__list-item, "
    "div[data-job-id]"
)

# Scrolls the Nth list-item shell into view, forcing LinkedIn to render it.
# Returns the current total number of list-item shells (which grows as more pages load).
_SCROLL_ITEM_JS = """
(args) => {
    const sel = args.sel, i = args.i;
    const items = document.querySelectorAll(sel);
    if (items[i]) items[i].scrollIntoView({ block: 'center' });
    return items.length;
}
"""

_EXTRACT_DESC_JS = """
() => {
    const selectors = [
        '#job-details',
        '.jobs-description__content',
        '.jobs-description-content__text',
        '.jobs-box__html-content',
        '.jobs-description',
        '.description__text',
        '.show-more-less-html__markup',
        '[class*="jobs-description"]',
        '[class*="description__text"]',
    ];
    for (const sel of selectors) {
        const el = document.querySelector(sel);
        if (el && el.innerText.trim().length > 50) {
            return { text: el.innerText.trim(), src: sel, bodyLen: document.body.innerText.length };
        }
    }
    // Fallback 1: the <main> region (job detail usually lives here)
    const main = document.querySelector('main');
    if (main && main.innerText.trim().length > 200) {
        return { text: main.innerText.trim(), src: 'main', bodyLen: document.body.innerText.length };
    }
    // Fallback 2: largest <section>/<article> text block on the page
    let best = '', bestSrc = '';
    document.querySelectorAll('article, section, div').forEach(el => {
        const t = el.innerText ? el.innerText.trim() : '';
        if (t.length > best.length && t.length < 20000) { best = t; bestSrc = el.tagName.toLowerCase(); }
    });
    if (best.length > 200) {
        return { text: best, src: 'largest:' + bestSrc, bodyLen: document.body.innerText.length };
    }
    // Nothing useful — report diagnostics
    return { text: '', src: 'NONE', bodyLen: document.body.innerText.length,
             title: document.title, url: location.href };
}
"""


_EASY_APPLY_JS = """
() => {
    // Detect Easy Apply for THIS job only. Scanning the whole page gives false
    // positives: the "More jobs for you" / "Similar jobs" cards at the bottom carry
    // their own Easy Apply badges. Strategy: find where that bottom section begins
    // (a Y coordinate), then only count an Easy Apply control that sits ABOVE it —
    // i.e. in proximity to the job we're actually viewing, near the top.
    const lower = (s) => (s || '').toLowerCase();
    const labelOf = (el) =>
        lower(el.innerText) + ' ' + lower(el.textContent) + ' ' + lower(el.getAttribute('aria-label'));

    // Where does the "other jobs" region start? Look at section headings.
    const markers = [
        'more jobs for you', 'similar jobs', 'people also viewed',
        'jobs you may be interested', 'jobs you might be interested',
        'recommended for you', 'people also applied',
    ];
    let boundary = Infinity;
    document.querySelectorAll('h2, h3').forEach((h) => {
        const t = lower(h.innerText);
        if (markers.some((m) => t.includes(m))) {
            const y = h.getBoundingClientRect().top + window.scrollY;
            if (y < boundary) boundary = y;
        }
    });
    const usedFallback = !isFinite(boundary);
    // If no marker heading found, guard with a fixed pixel cutoff. The real apply
    // CTA lives in the top card (~y<700); the similar-jobs cards are far below.
    // A fixed cutoff is more reliable than a % of body height, because LinkedIn
    // often renders the description in an inner scroll container, making
    // document.body.scrollHeight unexpectedly small.
    if (usedFallback) boundary = 700;

    let total = 0, above = 0;
    document.querySelectorAll('button, a').forEach((el) => {
        if (!labelOf(el).includes('easy apply')) return;
        total++;
        const y = el.getBoundingClientRect().top + window.scrollY;
        if (y < boundary) above++;
    });

    return { easy: above > 0, total: total, above: above,
             boundary: Math.round(boundary), fallback: usedFallback };
}
"""


def _clean_li_title(raw: str) -> str:
    """Strip LinkedIn aria-label noise like 'with verification' and trailing duplicates."""
    import re
    t = raw.strip()
    # Remove the "with verification" badge text LinkedIn injects into aria-labels
    t = re.sub(r"\s+with verification\b", "", t, flags=re.I)
    # aria-labels sometimes repeat the title twice — collapse "X X" → "X"
    half = len(t) // 2
    if t[:half].strip() and t[:half].strip() == t[half:].strip():
        t = t[:half].strip()
    return t.strip()


async def _extract_jobs_from_page(page, max_results: int, log, stop_event=None) -> list[dict]:
    """
    LinkedIn's results list is virtualized — it recycles DOM nodes, so only ~9
    job links exist at any instant. We scroll down one step at a time and harvest
    the currently-visible links after each step, accumulating unique jobs until we
    reach max_results or hit the bottom of the list.
    """
    collected: dict[str, dict] = {}   # href -> job (preserves insertion order)

    # Harvest whatever is already rendered before we start scrolling
    for job in await page.evaluate(_EXTRACT_JOBS_JS):
        collected.setdefault(job["href"], job)

    # Walk each list-item shell by index, scrolling it into view to force a render,
    # then harvest the now-visible job links. This is robust to DOM recycling and
    # doesn't depend on correctly identifying the scroll container.
    i = 0
    item_count = await page.evaluate(
        "(sel) => document.querySelectorAll(sel).length", _LIST_ITEM_SELECTOR
    )
    max_iters = max_results * 2 + 15
    while i < item_count and len(collected) < max_results and i < max_iters:
        if stop_event and stop_event.is_set():
            break
        item_count = await page.evaluate(
            _SCROLL_ITEM_JS, {"sel": _LIST_ITEM_SELECTOR, "i": i}
        )
        await page.wait_for_timeout(random.randint(350, 700))
        for job in await page.evaluate(_EXTRACT_JOBS_JS):
            collected.setdefault(job["href"], job)
        i += 1

    if not collected:
        title = await page.title()
        log(f"  WARNING: no jobs found. Page title: {title!r}")
        log(f"  This may indicate bot detection, a CAPTCHA, or a login redirect.")
        return []

    jobs = list(collected.values())
    log(f"  {len(jobs)} unique job link(s) collected from {item_count} shells, "
        f"processing up to {max_results}")
    return jobs[:max_results]


def _jobid_from_href(href: str) -> str:
    m = re.search(r"/jobs/view/(\d+)", href or "")
    return m.group(1) if m else ""


# Scrolls the Nth list shell into view AND reads its card info in one call.
# Returns {count, card:{href,title,company,location}|null}.
_SCROLL_AND_READ_JS = """
(args) => {
    const items = document.querySelectorAll(args.sel);
    const card = items[args.i];
    if (card) card.scrollIntoView({ block: 'center' });
    let info = null;
    if (card) {
        const a = card.querySelector('a[href*="/jobs/view/"]');
        if (a) {
            const href = a.href ? a.href.split('?')[0] : '';
            const title = (a.getAttribute('aria-label') || a.innerText || '').trim();
            const companyEl =
                card.querySelector('[class*="primary-description"]') ||
                card.querySelector('[class*="company-name"]') ||
                card.querySelector('[class*="subtitle"] span');
            const locEl =
                card.querySelector('[class*="metadata-item"]') ||
                card.querySelector('[class*="location"]');
            info = { href, title,
                     company: companyEl ? companyEl.innerText.trim() : '',
                     location: locEl ? locEl.innerText.trim() : '' };
        }
    }
    return { count: items.length, card: info };
}
"""


# Reads everything from the RIGHT DETAIL PANE of the jobs SEARCH view (the
# single-click target). Scoping to the pane is essential: it excludes the left
# results list (whose cards carry their own "Easy Apply" badges and titles).
_PANE_EXTRACT_JS = r"""
() => {
    const lower = (s) => (s || '').toLowerCase();
    const pane =
        document.querySelector('.jobs-search__job-details') ||
        document.querySelector('.jobs-search__job-details--container') ||
        document.querySelector('.scaffold-layout__detail') ||
        document.querySelector('.job-view-layout') ||
        document.querySelector('.jobs-details');
    const root = pane || document;

    // ---- Description (scoped to the pane) ----
    const descSelectors = ['#job-details', '.jobs-description__content',
        '.jobs-description-content__text', '.jobs-box__html-content',
        '.jobs-description', '.show-more-less-html__markup', '[class*="jobs-description"]'];
    let desc = '', descSrc = '';
    for (const sel of descSelectors) {
        const el = root.querySelector(sel);
        if (el && el.innerText.trim().length > 50) { desc = el.innerText.trim(); descSrc = sel; break; }
    }
    if (!desc && pane && pane.innerText.trim().length > 200) { desc = pane.innerText.trim(); descSrc = 'pane'; }

    // ---- Easy Apply (scoped to the pane → no left-list false positives) ----
    const hasEasy = (el) =>
        (lower(el.innerText) + ' ' + lower(el.textContent) + ' ' + lower(el.getAttribute('aria-label')))
            .includes('easy apply');
    let easy = false;
    const applyBtn = root.querySelector('button.jobs-apply-button, button[class*="jobs-apply-button"]');
    if (applyBtn) { easy = hasEasy(applyBtn); }
    else { for (const b of root.querySelectorAll('button')) { if (hasEasy(b)) { easy = true; break; } } }

    // ---- LinkedIn's own match assessment ----
    // Scan the WHOLE document (the assessment card may sit OUTSIDE the description
    // container; the phrase is specific enough that this is false-positive-safe).
    // Tolerant matcher: allows line breaks/inline spans, requires a match/missing
    // verb (so global-nav "Your profile" links can't trigger it), and the trailing
    // period is OPTIONAL (the sentence is a heading and often has none).
    const body = document.body.innerText || '';
    const m = body.match(/Your profile[a-z\s]{0,20}(?:match|matches|missing)[a-z\s]{0,30}qualifications(?:\s+well)?/i);
    let liMatch = '';
    if (m) {
        liMatch = m[0].replace(/\s+/g, ' ').trim();
        if (!liMatch.endsWith('.')) liMatch += '.';
    }
    const profSeen = /required qualifications/i.test(body);  // diagnostic
    const loading = !!document.querySelector('.artdeco-loader, [class*="loader"], [role="progressbar"], svg.artdeco-spinner');

    return { desc, descSrc, easy, liMatch, profSeen, loading, paneFound: !!pane };
}
"""


async def _click_card(page, i: int, href: str, log, stop_event=None) -> bool:
    """Single-click the i-th job card so it loads in the right detail pane (SPA,
    no full navigation). Returns True if the pane switched to this job."""
    jobid = _jobid_from_href(href)
    try:
        link = page.locator(_LIST_ITEM_SELECTOR).nth(i).locator("a[href*='/jobs/view/']").first
        await link.click(timeout=5000)
    except Exception:
        # Fallback: dispatch a click via JS on the shell's link.
        try:
            await page.evaluate(
                "(args) => { const it = document.querySelectorAll(args.sel)[args.i];"
                " const a = it && it.querySelector('a[href*=\"/jobs/view/\"]'); if (a) a.click(); }",
                {"sel": _LIST_ITEM_SELECTOR, "i": i},
            )
        except Exception:
            return False
    # Confirm the pane now shows this job (currentJobId in the URL).
    if jobid:
        try:
            await page.wait_for_function(
                "(id) => location.href.includes('currentJobId=' + id)",
                arg=jobid, timeout=8000,
            )
        except Exception:
            pass  # proceed anyway — extraction reads whatever the pane shows
    return True


async def _extract_from_pane(page, log, stop_event=None) -> tuple[str | None, bool, str | None]:
    """Read description + Easy Apply + LinkedIn match from the right pane after a
    card has been selected. POLLS for the async match assessment (3–7s spinner).
    Returns (description_or_None, has_easy_apply, linkedin_match_or_None)."""
    # Let the pane swap in the newly-selected job.
    await page.wait_for_timeout(random.randint(800, 1400))
    # Expand the description if a "see more" is present in the pane.
    try:
        btn = await page.query_selector("button[aria-label*='see more'], button.show-more-less-html__button")
        if btn:
            await btn.click()
            await page.wait_for_timeout(300)
    except Exception:
        pass

    MAX_WAIT_MS, GRACE_MS, POLL_MS = 9000, 2500, 500
    waited = 0
    last: dict = {}
    while True:
        if stop_event and stop_event.is_set():
            break
        try:
            last = await page.evaluate(_PANE_EXTRACT_JS) or {}
        except Exception:
            last = {}
        if (last.get("liMatch") or "").strip():
            log(f"  [li-match] ({waited/1000:.1f}s) {last['liMatch']}")
            break
        if waited >= MAX_WAIT_MS:
            break
        # Nothing loading and past the grace window → this job has no assessment.
        if not last.get("loading") and waited >= GRACE_MS:
            break
        await page.wait_for_timeout(POLL_MS)
        waited += POLL_MS

    desc     = (last.get("desc") or "").strip() or None
    easy     = bool(last.get("easy"))
    li_match = (last.get("liMatch") or "").strip() or None
    src      = last.get("descSrc", "?")
    if not li_match:
        # Diagnostic: if the page DID contain "required qualifications" but we still
        # captured nothing, the matcher needs tuning (tell us via the log).
        log(f"  [li-match] none (profileTextSeen={last.get('profSeen')}, "
            f"waited {waited/1000:.1f}s)")
    if desc:
        log(f"  [desc] {len(desc)} chars via {src}{' · Easy Apply' if easy else ''}")
    else:
        log(f"  [desc] EMPTY (paneFound={last.get('paneFound')})")
    return desc, easy, li_match


def _store_new_card(*, card, desc, easy, li_match, term, dry_run, counts, now, log) -> None:
    """Upsert + score a NEW job (caller already confirmed it's not in the DB)."""
    from db.models import upsert_job, STATUS_NEW, link_term_job
    from matching.resume_matcher import score_job_if_enabled
    href    = card["href"]
    title   = _clean_li_title(card["title"])
    company = card["company"] or None
    loc     = card["location"] or None
    label   = f"{title}" + (f" @ {company}" if company else "")
    if dry_run:
        log(f"  [dry] {label}{' · Easy Apply' if easy else ''}")
        counts["new"] += 1
        return
    job_id, status = upsert_job(
        source="linkedin", company=company, title=title, location=loc, url=href,
        description=desc, posted_at=None, fetched_at=now,
        easy_apply=int(easy), li_match=li_match,
    )
    if status == STATUS_NEW:
        score_status = score_job_if_enabled(job_id)
        if score_status.startswith("scored"):
            counts["scored"] += 1
        counts["new"] += 1
        log(f"  [NEW]      {label} — {score_status}")
    else:
        counts["existing"] += 1
        log(f"  [EXISTS]   {label} — already in database, skipping")
    if term:
        link_term_job(term, job_id)


async def _scrape_pages_by_clicking(page, *, base_url, max_results, term,
                                    dry_run, counts, now, log, stop_event=None) -> None:
    """Merged collect+process pass. For each search page, single-click each job
    card to load the right pane (where LinkedIn shows the match assessment), then
    extract + store. Existing jobs are NOT re-scored (saves API cost), but if a row
    is missing LinkedIn's match assessment it IS clicked once to backfill it (free)."""
    from db.models import (get_job_id_status, get_job, upsert_job,
                           STATUS_APPLIED, link_term_job)
    from scrapers._config import is_blacklisted_title, is_blacklisted_company
    from app_settings import get_backfill_li_match
    backfill_li = get_backfill_li_match()   # opt-in: capture li_match on existing jobs

    seen: set[str] = set()
    start = 0
    while len(seen) < max_results:
        if stop_event and stop_event.is_set():
            break
        await page.goto(f"{base_url}&start={start}", wait_until="domcontentloaded")
        await page.wait_for_timeout(random.randint(2500, 5000))
        for _ in range(2):   # nudge the virtualized list to render
            await page.keyboard.press("End")
            await page.wait_for_timeout(random.randint(900, 1600))

        try:
            count = await page.evaluate(
                "(sel) => document.querySelectorAll(sel).length", _LIST_ITEM_SELECTOR)
        except Exception:
            count = 0

        gained = 0
        i = 0
        iters = 0
        max_iters = max_results * 3 + 20
        while i < count and len(seen) < max_results and iters < max_iters:
            iters += 1
            if stop_event and stop_event.is_set():
                log("  Stopped by user.")
                break
            try:
                res = await page.evaluate(_SCROLL_AND_READ_JS, {"sel": _LIST_ITEM_SELECTOR, "i": i})
            except Exception:
                res = {}
            count = res.get("count", count) or count
            card = res.get("card")
            await page.wait_for_timeout(random.randint(300, 650))
            if not card or not card.get("href"):
                i += 1
                continue
            href = card["href"]
            if href in seen:
                i += 1
                continue
            seen.add(href)
            gained += 1

            title   = _clean_li_title(card["title"])
            company = card["company"] or None
            label   = f"{title}" + (f" @ {company}" if company else "")

            if is_blacklisted_title(title):
                log(f"  [SKIP]     {title!r} — blacklisted")
                counts["blacklisted"] += 1
                i += 1
                continue
            if is_blacklisted_company(company):
                log(f"  [SKIP]     {company!r} — blacklisted company")
                counts["blacklisted"] += 1
                i += 1
                continue

            # Already have it? Count + link, and skip the (paid) re-scoring. BUT if the
            # row is missing LinkedIn's match assessment, click it to capture that —
            # it's free (no API) and the "matches well" tier often shows on recurring
            # existing jobs we'd otherwise never read. Once captured, future runs skip.
            existing = get_job_id_status(href)
            if existing:
                job_id, status = existing
                if backfill_li and not (get_job(job_id)["li_match"] or "").strip():
                    await page.wait_for_timeout(random.randint(400, 900))
                    if await _click_card(page, i, href, log, stop_event):
                        _d, _e, li_match = await _extract_from_pane(page, log, stop_event)
                        if li_match:
                            upsert_job(source="linkedin", company=company, title=title,
                                       location=None, url=href, description=None,
                                       posted_at=None, fetched_at=now, li_match=li_match)
                            log(f"  [li+]      {label} — assessment captured")
                if status == STATUS_APPLIED:
                    counts["reviewed"] += 1
                    log(f"  [REVIEWED] {label} — already applied")
                else:
                    counts["existing"] += 1
                    log(f"  [EXISTS]   {label} — already in database")
                if term:
                    link_term_job(term, job_id)
                i += 1
                continue

            # NEW → single-click the card, read the right pane, store.
            await page.wait_for_timeout(random.randint(500, 1200))
            ok = await _click_card(page, i, href, log, stop_event)
            if not ok:
                log(f"  [warn] could not open card {i}")
                i += 1
                continue
            desc, easy, li_match = await _extract_from_pane(page, log, stop_event)
            _store_new_card(card=card, desc=desc, easy=easy, li_match=li_match,
                            term=term, dry_run=dry_run, counts=counts, now=now, log=log)
            i += 1

        log(f"  page start={start}: +{gained} card(s) seen (total examined {len(seen)})")
        if gained == 0:        # nothing new on this page → end of results
            break
        start += 25


async def save_session() -> None:
    """Launch a headed browser so the user can log in manually, then save context."""
    SESSION_DIR.mkdir(exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch_persistent_context(
            str(SESSION_DIR),
            headless=False,
            args=["--start-maximized"],
        )
        page = browser.pages[0] if browser.pages else await browser.new_page()
        await page.goto("https://www.linkedin.com/login")
        print("Please log in to LinkedIn in the browser window.")
        print("After you are fully logged in, press Enter here to save the session...")
        input()
        await browser.close()
    print(f"Session saved to {SESSION_DIR}")


async def scrape_async(dry_run: bool = False, lookback_hours: int | None = None, stop_event=None, log_fn=None) -> dict:
    log = log_fn or print
    counts = {"new": 0, "scored": 0, "existing": 0, "reviewed": 0, "blacklisted": 0}

    from scrapers._config import load_search_config
    yaml_cfg = _load_config()
    toml_cfg = load_search_config()

    li_yaml = yaml_cfg.get("linkedin", {})
    li_toml = toml_cfg.get("linkedin", {})

    search_terms = li_toml.get("search_terms", [])
    location     = li_toml.get("location", "")
    max_results  = li_toml.get("max_results_per_term", 25)
    remote_only  = li_toml.get("remote_only", False)
    hours = lookback_hours if lookback_hours is not None else li_yaml.get("lookback_hours", 24)

    # f_WT=2 → Remote work type filter (LinkedIn's correct remote parameter)
    work_type_param = "&f_WT=2" if remote_only else ""
    location_label  = f"{location} (Remote)" if remote_only else location

    now = datetime.now().isoformat()

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            str(SESSION_DIR),
            headless=False,   # set to True once confirmed working; False avoids blocks
        )
        page = context.pages[0] if context.pages else await context.new_page()

        for i, term in enumerate(search_terms):
            if stop_event and stop_event.is_set():
                print("[linkedin] stop requested, halting.")
                break
            # Pause between search terms (skip before the first one)
            if i > 0:
                delay = random.randint(4000, 9000)
                log(f"  Pausing {delay//1000}s before next search…")
                await page.wait_for_timeout(delay)
            log(f"  Searching: {term!r} — {location_label}")
            encoded_term = term.replace(" ", "%20")
            encoded_loc = location.replace(" ", "%20").replace(",", "%2C")
            tpr_seconds = hours * 3600
            base_url = (
                f"https://www.linkedin.com/jobs/search/"
                f"?keywords={encoded_term}&location={encoded_loc}"
                f"&f_TPR=r{tpr_seconds}{work_type_param}"
            )
            # Click each card in the results pane (where LinkedIn shows the match
            # assessment). term=None → bulk run doesn't record per-term stats.
            await _scrape_pages_by_clicking(
                page, base_url=base_url, max_results=max_results, term=None,
                dry_run=dry_run, counts=counts, now=now, log=log, stop_event=stop_event,
            )

        await context.close()

    return counts


def scrape(dry_run: bool = False, lookback_hours: int | None = None, stop_event=None, log_fn=None) -> dict:
    return asyncio.run(scrape_async(dry_run=dry_run, lookback_hours=lookback_hours, stop_event=stop_event, log_fn=log_fn))


# ---------------------------------------------------------------------------
# Single-term scrape (used by the LinkedIn Terms page)
# ---------------------------------------------------------------------------

async def _scrape_one_term_async(term: str, hours: int, max_results: int,
                                  stop_event=None, log_fn=None) -> dict:
    from scrapers._config import get_linkedin_defaults
    log = log_fn or print
    counts = {"new": 0, "scored": 0, "existing": 0, "reviewed": 0, "blacklisted": 0}

    defaults = get_linkedin_defaults()
    location    = defaults["location"]
    remote_only = defaults["remote_only"]
    work_type_param = "&f_WT=2" if remote_only else ""
    location_label  = f"{location} (Remote)" if remote_only else location

    now = datetime.now().isoformat()

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            str(SESSION_DIR), headless=False,
        )
        page = context.pages[0] if context.pages else await context.new_page()

        log(f"  Searching: {term!r} — {location_label}, last {hours}h, max {max_results}")
        encoded_term = term.replace(" ", "%20")
        encoded_loc  = location.replace(" ", "%20").replace(",", "%2C")
        tpr_seconds  = hours * 3600
        base_url = (
            f"https://www.linkedin.com/jobs/search/"
            f"?keywords={encoded_term}&location={encoded_loc}"
            f"&f_TPR=r{tpr_seconds}{work_type_param}"
        )

        # Single-click each card in the results pane (so LinkedIn's match assessment
        # renders), extract from the right pane, store. term=term records per-term stats.
        await _scrape_pages_by_clicking(
            page, base_url=base_url, max_results=max_results, term=term,
            dry_run=False, counts=counts, now=now, log=log, stop_event=stop_event,
        )

        await context.close()

    return counts


def scrape_single_term(term: str, hours: int = 24, max_results: int = 25,
                        stop_event=None, log_fn=None) -> dict:
    return asyncio.run(_scrape_one_term_async(
        term=term, hours=hours, max_results=max_results,
        stop_event=stop_event, log_fn=log_fn,
    ))


if __name__ == "__main__":
    if "--save-session" in sys.argv:
        asyncio.run(save_session())
    else:
        scrape(dry_run="--dry-run" in sys.argv)
