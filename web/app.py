"""
Flask web application for JobScrape.
"""
import threading
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, render_template, request

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from db.models import (
    delete_source,
    get_job,
    list_jobs,
    list_sources,
    add_source,
    toggle_applied,
    toggle_expired,
    toggle_hidden,
    toggle_response_received,
    toggle_source_active,
    update_notes,
    update_location,
    set_match,
)

app = Flask(__name__, template_folder="templates", static_folder="static")

# Persistent per-term run history (survives app restarts)
import json
_RUNS_PATH = Path(__file__).parent.parent / "linkedin_runs.json"
_runs_lock = threading.Lock()


def _load_runs() -> dict:
    try:
        return json.loads(_RUNS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_run(term: str, counts: dict, when: str) -> None:
    """Accumulate this run's counts into the term's cumulative lifetime totals."""
    with _runs_lock:
        runs = _load_runs()
        entry = runs.get(term, {})

        totals = entry.get("totals", {"new": 0, "existing": 0, "reviewed": 0, "blacklisted": 0})
        for key in ("new", "existing", "reviewed", "blacklisted"):
            totals[key] = totals.get(key, 0) + counts.get(key, 0)

        runs[term] = {
            "last_run":    when,
            "last_counts": counts,                  # most recent run's per-type counts
            "run_count":   entry.get("run_count", 0) + 1,
            "totals":      totals,                  # lifetime per-run event totals
        }
        # NOTE: distinct job count + average score are NOT stored here — they are
        # computed live from the term_jobs table so each job counts once per term.
        try:
            _RUNS_PATH.write_text(json.dumps(runs, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"[runs] failed to save run history: {e}")


# ---------------------------------------------------------------------------
# Scraper state (in-memory, single-user local app)
# ---------------------------------------------------------------------------

_scraper_state: dict = {
    "running": False,
    "stopped": False,
    "log": [],
    "last_run": None,
    "counts": {},
}
_scraper_lock = threading.Lock()
_stop_event = threading.Event()

# Per-term scrape state for the LinkedIn Terms page
_term_scrapes: dict[str, dict] = {}
_term_scrapes_lock = threading.Lock()
_term_stop_events: dict[str, threading.Event] = {}

# Cache the last email list load so it survives page navigation
_email_cache: dict = {
    "emails": [],
    "days": 14,
    "loaded_at": None,
}

# Bulk re-score (selected rows → Rescore) runs in a daemon thread with a poller.
_rescore_state: dict = {
    "running": False, "total": 0, "done": 0, "ok": 0, "fail": 0,
    "backend": "", "started": None, "last_error": "",
}
_rescore_lock = threading.Lock()


def _run_bulk_rescore(job_ids: list[int], backend: str) -> None:
    from matching.resume_matcher import rescore_job
    try:
        for jid in job_ids:
            with _rescore_lock:
                if not _rescore_state["running"]:
                    break  # stopped by user
            status = rescore_job(jid, backend)          # network call OUTSIDE the lock
            print(f"[bulk-rescore] job {jid} ({backend}): {status}")
            with _rescore_lock:
                _rescore_state["done"] += 1
                if status.startswith("scored"):
                    _rescore_state["ok"] += 1
                else:
                    _rescore_state["fail"] += 1
                    _rescore_state["last_error"] = status
    finally:
        with _rescore_lock:
            _rescore_state["running"] = False


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with _scraper_lock:
        _scraper_state["log"].append(line)


def _fmt_counts(counts: dict) -> str:
    """Format a counts dict into a human-readable summary line."""
    parts = []
    if counts.get("new", 0):
        scored = counts.get("scored", 0)
        parts.append(f"{counts['new']} new ({scored} scored by AI)")
    if counts.get("existing", 0):
        parts.append(f"{counts['existing']} already in database")
    if counts.get("reviewed", 0):
        parts.append(f"{counts['reviewed']} already reviewed/applied")
    if counts.get("blacklisted", 0):
        parts.append(f"{counts['blacklisted']} blacklisted")
    return ", ".join(parts) if parts else "0 listings processed"


def _run_scrapers_background(run_gmail: bool, run_linkedin: bool, run_career: bool,
                              gmail_days: int, linkedin_hours: int) -> None:
    _stop_event.clear()
    with _scraper_lock:
        _scraper_state["running"] = True
        _scraper_state["stopped"] = False
        _scraper_state["log"] = []
        _scraper_state["counts"] = {}

    try:
        if run_gmail and not _stop_event.is_set():
            _log(f"Starting Gmail scraper (last {gmail_days} days)…")
            try:
                from scrapers.gmail_scraper import scrape as gmail_scrape
                c = gmail_scrape(lookback_days=gmail_days, stop_event=_stop_event, log_fn=_log)
                summary = _fmt_counts(c)
                _log(f"Gmail {'stopped early' if _stop_event.is_set() else 'done'} — {summary}.")
                with _scraper_lock:
                    _scraper_state["counts"]["gmail"] = c
            except Exception as e:
                _log(f"Gmail error: {e}")

        if run_linkedin and not _stop_event.is_set():
            _log(f"Starting LinkedIn scraper (last {linkedin_hours} hours)…")
            try:
                from scrapers.linkedin_scraper import scrape as li_scrape
                c = li_scrape(lookback_hours=linkedin_hours, stop_event=_stop_event, log_fn=_log)
                summary = _fmt_counts(c)
                _log(f"LinkedIn {'stopped early' if _stop_event.is_set() else 'done'} — {summary}.")
                with _scraper_lock:
                    _scraper_state["counts"]["linkedin"] = c
            except Exception as e:
                _log(f"LinkedIn error: {e}")

        if run_career and not _stop_event.is_set():
            _log("Starting career page scraper…")
            try:
                from scrapers.career_page_scraper import scrape as cp_scrape
                c = cp_scrape(stop_event=_stop_event, log_fn=_log)
                summary = _fmt_counts(c)
                _log(f"Career pages {'stopped early' if _stop_event.is_set() else 'done'} — {summary}.")
                with _scraper_lock:
                    _scraper_state["counts"]["career_pages"] = c
            except Exception as e:
                _log(f"Career pages error: {e}")

        if _stop_event.is_set():
            _log("Scrape aborted by user.")
        else:
            _log("All selected scrapers finished.")
    finally:
        with _scraper_lock:
            _scraper_state["running"] = False
            _scraper_state["stopped"] = _stop_event.is_set()
            _scraper_state["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _score_color(score: int | None) -> str:
    if score is None:
        return "unscored"
    if score >= 70:
        return "green"
    if score >= 40:
        return "yellow"
    return "red"


def _li_match_badge(text: str | None) -> dict:
    """Turn LinkedIn's raw match sentence into a short label + color tier."""
    if not text:
        return {"label": None, "tier": "none"}
    t = text.lower()
    if "qualifications well" in t:
        return {"label": "Strong match", "tier": "green"}
    if "several" in t:
        return {"label": "Several match", "tier": "green"}
    if "some" in t:
        return {"label": "Some match", "tier": "yellow"}
    if "is missing" in t:
        return {"label": "Missing quals", "tier": "red"}
    return {"label": "Match info", "tier": "yellow"}


def _default_lookbacks() -> tuple[int, int]:
    import yaml
    cfg = yaml.safe_load((Path(__file__).parent.parent / "config.yaml").read_text())
    gmail_days = cfg.get("gmail", {}).get("lookback_days", 7)
    li_hours = cfg.get("linkedin", {}).get("lookback_hours", 24)
    return gmail_days, li_hours


def _parse_sort(sort_param: str) -> tuple[str, str]:
    """Split 'match_score DESC' → ('match_score', 'DESC')."""
    parts = sort_param.rsplit(" ", 1)
    if len(parts) == 2 and parts[1].upper() in ("ASC", "DESC"):
        return parts[0], parts[1].upper()
    return sort_param, "DESC"


# ---------------------------------------------------------------------------
# Job listing routes
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    applied_filter  = request.args.get("applied", "")
    response_filter = request.args.get("response", "")
    min_score = request.args.get("min_score", type=int)
    sort = request.args.get("sort", "fetched_at DESC")

    expired_filter  = request.args.get("expired", "no")  # default: hide expired
    hidden_filter   = request.args.get("hidden", "no")   # default: hide dismissed
    li_match_filter = request.args.get("li_match", "")   # LinkedIn match category

    applied_val  = {"yes": 1, "no": 0}.get(applied_filter)
    response_val = {"yes": 1, "no": 0}.get(response_filter)
    # "all" → pass None (no filter); "yes" → 1; "no" → 0
    expired_val  = {"yes": 1, "no": 0}.get(expired_filter)  # None when "all"
    hidden_val   = {"yes": 1, "no": 0}.get(hidden_filter)   # None when "all"

    jobs = list_jobs(applied=applied_val, response_received=response_val,
                     expired=expired_val, hidden=hidden_val,
                     min_score=min_score, li_match=li_match_filter or None,
                     order_by=sort)
    rows = [dict(j) | {"score_color": _score_color(j["match_score"]),
                       "local_color": _score_color(j["local_score"]),
                       "li_badge": _li_match_badge(j["li_match"])} for j in jobs]

    sort_col, sort_dir = _parse_sort(sort)

    return render_template("index.html", jobs=rows,
                           applied_filter=applied_filter,
                           response_filter=response_filter,
                           expired_filter=expired_filter,
                           hidden_filter=hidden_filter,
                           li_match_filter=li_match_filter,
                           min_score=min_score,
                           sort=sort,
                           sort_col=sort_col,
                           sort_dir=sort_dir)


@app.get("/jobs/<int:job_id>")
def detail(job_id: int):
    job = get_job(job_id)
    if job is None:
        return "Not found", 404
    row = dict(job) | {"score_color": _score_color(job["match_score"]),
                       "local_color": _score_color(job["local_score"])}
    return render_template("detail.html", job=row)


@app.post("/jobs/<int:job_id>/toggle-applied")
def toggle_applied_route(job_id: int):
    new_val = toggle_applied(job_id)
    checked = "checked" if new_val else ""
    return f"""<input type="checkbox" {checked}
        hx-post="/jobs/{job_id}/toggle-applied"
        hx-swap="outerHTML"
        title="Mark as applied">"""


@app.post("/jobs/<int:job_id>/toggle-expired")
def toggle_expired_route(job_id: int):
    new_val = toggle_expired(job_id)
    checked = "checked" if new_val else ""
    return f"""<input type="checkbox" {checked}
        hx-post="/jobs/{job_id}/toggle-expired"
        hx-swap="outerHTML"
        title="Listing expired / no longer accepting applications">"""


@app.post("/jobs/<int:job_id>/toggle-hidden")
def toggle_hidden_route(job_id: int):
    new_val = toggle_hidden(job_id)
    checked = "checked" if new_val else ""
    return f"""<input type="checkbox" {checked}
        hx-post="/jobs/{job_id}/toggle-hidden"
        hx-swap="outerHTML"
        title="Hide listing — junk / scam / broken link (kept in DB, won't be re-added)">"""


@app.post("/jobs/<int:job_id>/toggle-response")
def toggle_response_route(job_id: int):
    new_val = toggle_response_received(job_id)
    checked = "checked" if new_val else ""
    return f"""<input type="checkbox" {checked}
        hx-post="/jobs/{job_id}/toggle-response"
        hx-swap="outerHTML"
        title="Response received">"""


@app.post("/jobs/<int:job_id>/notes")
def save_notes(job_id: int):
    notes = request.form.get("notes", "")
    update_notes(job_id, notes)
    return '<span class="saved-indicator">Saved ✓</span>'


@app.post("/jobs/<int:job_id>/location")
def save_location(job_id: int):
    location = request.form.get("location", "").strip()
    update_location(job_id, location)
    return '<span class="saved-indicator">Saved ✓</span>'


@app.post("/jobs/<int:job_id>/rescore")
def rescore(job_id: int):
    from matching.resume_matcher import score_job
    backend = request.form.get("backend", "claude")
    job = get_job(job_id)
    if job is None:
        return "Not found", 404
    if not job["description"]:
        return '<span class="error">No description to score.</span>'
    result = score_job(job_id, job["description"], job["title"], job["company"], backend)
    if result:
        score, reason = result
        set_match(job_id, score, reason, backend)
        color = _score_color(score)
        return render_template("_score_bar.html", score=score, color=color, reason=reason)
    engine = "local LLM" if backend == "local" else "Claude API"
    return f'<span class="error">Scoring failed — check {engine}.</span>'


@app.post("/jobs/bulk-rescore")
def bulk_rescore():
    backend = request.form.get("backend", "local")
    if backend not in ("claude", "local"):
        backend = "local"
    try:
        job_ids = [int(x) for x in request.form.getlist("job_ids")]
    except ValueError:
        job_ids = []

    with _rescore_lock:
        if _rescore_state["running"]:
            return render_template("_rescore_status.html", st=dict(_rescore_state))
        if not job_ids:
            return render_template("_rescore_status.html",
                                   st={"running": False, "total": 0, "done": 0,
                                       "ok": 0, "fail": 0, "backend": backend,
                                       "empty": True})
        _rescore_state.update({"running": True, "total": len(job_ids), "done": 0,
                               "ok": 0, "fail": 0, "backend": backend, "last_error": ""})

    threading.Thread(target=_run_bulk_rescore, args=(job_ids, backend), daemon=True).start()
    with _rescore_lock:
        return render_template("_rescore_status.html", st=dict(_rescore_state))


@app.get("/jobs/rescore-status")
def rescore_status():
    with _rescore_lock:
        return render_template("_rescore_status.html", st=dict(_rescore_state))


@app.post("/jobs/bulk-rescore/stop")
def bulk_rescore_stop():
    with _rescore_lock:
        _rescore_state["running"] = False
        return render_template("_rescore_status.html", st=dict(_rescore_state))


# ---------------------------------------------------------------------------
# Scraper control routes
# ---------------------------------------------------------------------------

@app.get("/run")
def run_page():
    from app_settings import get_settings
    gmail_days, li_hours = _default_lookbacks()
    with _scraper_lock:
        state = dict(_scraper_state)
    return render_template("run.html", state=state,
                           default_gmail_days=gmail_days,
                           default_li_hours=li_hours,
                           email_cache=_email_cache,
                           s=get_settings())


@app.post("/settings/toggle-scoring")
def toggle_scoring():
    from app_settings import get_settings, set_scoring_enabled
    # checkbox present in form data => enabled
    set_scoring_enabled("scoring_enabled" in request.form)
    return render_template("_scoring_settings.html", s=get_settings())


@app.post("/settings/scoring-backend")
def scoring_backend():
    from app_settings import get_settings, set_scoring_backend, set_rationale_detail
    set_scoring_backend(
        request.form.get("scoring_backend", "claude"),
        base_url=request.form.get("local_base_url", ""),
        model=request.form.get("local_model", ""),
    )
    set_rationale_detail(request.form.get("rationale_detail", "detailed"))
    return render_template("_scoring_settings.html", s=get_settings(), saved=True)


@app.post("/settings/detect-models")
def detect_models():
    from matching.resume_matcher import list_local_models
    ok, res = list_local_models(request.form.get("local_base_url", ""))
    if not ok:
        return f'<span class="test-result err">{res}</span>'
    return render_template("_model_detect.html", models=res, first=res[0])


@app.post("/settings/test-local")
def test_local():
    from matching.resume_matcher import test_local_connection
    ok, msg = test_local_connection(
        request.form.get("local_base_url", ""),
        request.form.get("local_model", ""),
    )
    cls = "ok" if ok else "err"
    return f'<span id="local-test-result" class="test-result {cls}">{msg}</span>'


@app.post("/run-scrapers")
def run_scrapers():
    with _scraper_lock:
        if _scraper_state["running"]:
            return render_template("_scraper_status.html",
                                   state=dict(_scraper_state)), 200

    run_gmail    = "gmail"    in request.form
    run_linkedin = "linkedin" in request.form
    run_career   = "career"   in request.form
    gmail_days     = int(request.form.get("gmail_days",     7))
    linkedin_hours = int(request.form.get("linkedin_hours", 24))

    t = threading.Thread(
        target=_run_scrapers_background,
        args=(run_gmail, run_linkedin, run_career, gmail_days, linkedin_hours),
        daemon=True,
    )
    t.start()

    with _scraper_lock:
        state = dict(_scraper_state)
        state["running"] = True
    return render_template("_scraper_status.html", state=state)


@app.get("/email-list")
def email_list():
    try:
        from scrapers.gmail_scraper import list_recent_job_emails
        days = request.args.get("days", 14, type=int)
        emails = list_recent_job_emails(lookback_days=days)
        _email_cache["emails"] = emails
        _email_cache["days"] = days
        _email_cache["loaded_at"] = datetime.now().strftime("%H:%M")
        return render_template("_email_list.html", emails=emails, days=days,
                               loaded_at=_email_cache["loaded_at"], error=None)
    except Exception as e:
        return render_template("_email_list.html", emails=[], days=14,
                               loaded_at=None, error=str(e))


@app.post("/scrape-email/<message_id>")
def scrape_email(message_id: str):
    try:
        from scrapers.gmail_scraper import scrape_message_by_id
        count, titles = scrape_message_by_id(message_id)
        return render_template("_email_scrape_result.html", count=count, titles=titles, error=None)
    except Exception as e:
        return render_template("_email_scrape_result.html", count=0, titles=[], error=str(e))


@app.post("/stop-scrapers")
def stop_scrapers():
    _stop_event.set()
    _log("Stop requested — finishing current item then halting…")
    with _scraper_lock:
        state = dict(_scraper_state)
    return render_template("_scraper_status.html", state=state)


@app.get("/scraper-status")
def scraper_status():
    with _scraper_lock:
        state = dict(_scraper_state)
    return render_template("_scraper_status.html", state=state)


# ---------------------------------------------------------------------------
# LinkedIn search terms page
# ---------------------------------------------------------------------------

def _term_log(term: str, msg: str) -> None:
    from datetime import datetime
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with _term_scrapes_lock:
        if term in _term_scrapes:
            _term_scrapes[term]["log"].append(line)


def _run_term_scrape(term: str, hours: int, max_results: int) -> None:
    stop_ev = _term_stop_events.setdefault(term, threading.Event())
    stop_ev.clear()
    with _term_scrapes_lock:
        _term_scrapes[term] = {"running": True, "log": [], "counts": {}, "last_run": None}
    try:
        from scrapers.linkedin_scraper import scrape_single_term
        from datetime import datetime
        counts = scrape_single_term(
            term=term, hours=hours, max_results=max_results,
            stop_event=stop_ev,
            log_fn=lambda msg: _term_log(term, msg),
        )
        when = datetime.now().strftime("%Y-%m-%d %H:%M")
        # Update counts/last_run under the lock, but do NOT call _term_log here —
        # _term_log acquires the same (non-reentrant) lock and would deadlock.
        with _term_scrapes_lock:
            _term_scrapes[term]["counts"] = counts
            _term_scrapes[term]["last_run"] = when
        # Log the final status OUTSIDE the lock (each _term_log manages its own lock)
        if stop_ev.is_set():
            _term_log(term, "Stopped by user.")
        else:
            _term_log(term, f"Done — {_fmt_counts(counts)}.")
        # Persist the run summary so it survives restarts and shows on the card
        _save_run(term, counts, when)
    except Exception as e:
        _term_log(term, f"Error: {e}")
    finally:
        with _term_scrapes_lock:
            _term_scrapes[term]["running"] = False


def _term_db_stats(terms: list[str]) -> dict:
    """Live, dedup-safe distinct-found + average-score per term, from the DB."""
    from db.models import term_score_stats
    return {t: term_score_stats(t) for t in terms}


@app.get("/linkedin-terms")
def linkedin_terms():
    from scrapers._config import get_linkedin_terms, get_linkedin_defaults, get_blacklist_companies
    terms = get_linkedin_terms()
    defaults = get_linkedin_defaults()
    with _term_scrapes_lock:
        states = dict(_term_scrapes)
    return render_template("linkedin_terms.html",
                           terms=terms, defaults=defaults, states=states,
                           runs=_load_runs(), stats=_term_db_stats(terms),
                           companies=get_blacklist_companies())


@app.post("/linkedin-terms/add")
def add_linkedin_term():
    from scrapers._config import get_linkedin_terms, add_linkedin_term as _add, get_linkedin_defaults
    term = request.form.get("term", "").strip()
    if term:
        _add(term)
    terms = get_linkedin_terms()
    with _term_scrapes_lock:
        states = dict(_term_scrapes)
    return render_template("_linkedin_terms_list.html",
                           terms=terms,
                           defaults=get_linkedin_defaults(),
                           states=states, runs=_load_runs(), stats=_term_db_stats(terms))


@app.post("/linkedin-terms/delete")
def delete_linkedin_term():
    from scrapers._config import get_linkedin_terms, remove_linkedin_term as _remove, get_linkedin_defaults
    term = request.form.get("term", "").strip()
    if term:
        _remove(term)
    terms = get_linkedin_terms()
    with _term_scrapes_lock:
        states = dict(_term_scrapes)
    return render_template("_linkedin_terms_list.html",
                           terms=terms,
                           defaults=get_linkedin_defaults(),
                           states=states, runs=_load_runs(), stats=_term_db_stats(terms))


# ---------------------------------------------------------------------------
# Company blacklist (global, all sources)
# ---------------------------------------------------------------------------

@app.post("/company-blacklist/add")
def add_company_blacklist():
    from scrapers._config import add_blacklist_company, get_blacklist_companies
    company = request.form.get("company", "").strip()
    if company:
        add_blacklist_company(company)
    return render_template("_company_blacklist.html",
                           companies=get_blacklist_companies())


@app.post("/company-blacklist/delete")
def delete_company_blacklist():
    from scrapers._config import remove_blacklist_company, get_blacklist_companies
    company = request.form.get("company", "").strip()
    if company:
        remove_blacklist_company(company)
    return render_template("_company_blacklist.html",
                           companies=get_blacklist_companies())


@app.post("/scrape-linkedin-term")
def scrape_linkedin_term():
    term        = request.form.get("term", "").strip()
    hours       = int(request.form.get("hours", 24))
    max_results = int(request.form.get("max_results", 25))
    slug        = request.form.get("slug", "1")
    if not term:
        return '<span class="error">No term specified.</span>'

    with _term_scrapes_lock:
        if _term_scrapes.get(term, {}).get("running"):
            state = _term_scrapes[term]
            return render_template("_term_scrape_status.html", term=term, state=state, slug=slug)

    t = threading.Thread(target=_run_term_scrape, args=(term, hours, max_results), daemon=True)
    t.start()

    state = {"running": True, "log": [], "counts": {}, "last_run": None}
    return render_template("_term_scrape_status.html", term=term, state=state, slug=slug)


@app.post("/stop-linkedin-term")
def stop_linkedin_term():
    term = request.form.get("term", "").strip()
    slug = request.form.get("slug", "1")
    ev = _term_stop_events.get(term)
    if ev:
        ev.set()
        _term_log(term, "Stop requested — halting after the current item…")
    with _term_scrapes_lock:
        if term in _term_scrapes and _term_scrapes[term].get("running"):
            _term_scrapes[term]["stopping"] = True
        state = dict(_term_scrapes.get(term, {}))
    return render_template("_term_scrape_status.html", term=term, state=state, slug=slug)


@app.get("/linkedin-term-status")
def linkedin_term_status():
    term = request.args.get("term", "")
    slug = request.args.get("slug", "1")
    with _term_scrapes_lock:
        state = dict(_term_scrapes.get(term, {}))
    return render_template("_term_scrape_status.html", term=term, state=state, slug=slug)


# ---------------------------------------------------------------------------
# Sources (career page URL management)
# ---------------------------------------------------------------------------

@app.get("/sources")
def sources():
    rows = list_sources()
    return render_template("sources.html", sources=[dict(s) for s in rows])


@app.post("/sources/add")
def add_source_route():
    company = request.form.get("company", "").strip()
    url = request.form.get("url", "").strip()
    selector = request.form.get("selector", "").strip() or None
    if company and url:
        add_source(company, url, selector)
    rows = list_sources()
    return render_template("_sources_list.html", sources=[dict(s) for s in rows])


@app.post("/sources/<int:source_id>/toggle")
def toggle_source(source_id: int):
    toggle_source_active(source_id)
    rows = list_sources()
    return render_template("_sources_list.html", sources=[dict(s) for s in rows])


@app.post("/sources/<int:source_id>/delete")
def delete_source_route(source_id: int):
    delete_source(source_id)
    rows = list_sources()
    return render_template("_sources_list.html", sources=[dict(s) for s in rows])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
