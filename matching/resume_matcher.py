"""
Resume matcher — uses the Claude API to score a job listing against the user's resume.

Resume is read from resume.txt in the project root.
Score is 0–100. Rationale is 2–3 sentences.

Run to score all unscored listings:
    python -m matching.resume_matcher --score-all
"""
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).parent.parent
RESUME_PATH = ROOT / "resume.txt"

_resume_cache: str | None = None


def _get_resume() -> str | None:
    global _resume_cache
    if _resume_cache is not None:
        return _resume_cache
    if not RESUME_PATH.exists():
        print("[matcher] resume.txt not found — skipping scoring. Create resume.txt in the project root.")
        return None
    _resume_cache = RESUME_PATH.read_text(encoding="utf-8").strip()
    return _resume_cache


def _load_config() -> dict:
    import yaml
    return yaml.safe_load((ROOT / "config.yaml").read_text())


# How verbose the rationale should be — chosen in the UI (Run Scrapers → Scoring).
_RATIONALE_INSTRUCTIONS = {
    "brief": "exactly 1–2 SHORT sentences (under 160 characters total). Lead with the "
             "strongest match signal, then the biggest gap.",
    "standard": "2–3 sentences. Summarize the strongest matches, then the main gaps the "
                "role requires.",
    "detailed": "4–6 sentences giving constructive, specific feedback. Name the key "
                "required skills, tools, or qualifications that are MISSING from the resume "
                "(so the candidate knows exactly what to add or emphasize), and note which "
                "important requirements the candidate clearly meets. Refer to concrete "
                "technologies and experience by name rather than speaking generally.",
}
_RATIONALE_MAX_TOKENS = {"brief": 200, "standard": 450, "detailed": 800}


def _build_prompt(resume: str, description: str, title: str | None,
                  company: str | None, detail: str = "detailed") -> str:
    reason_instr = _RATIONALE_INSTRUCTIONS.get(detail, _RATIONALE_INSTRUCTIONS["standard"])
    return f"""You are evaluating how well a candidate's resume matches a job listing.

RESUME:
{resume}

JOB LISTING:
Title: {title or 'Unknown'}
Company: {company or 'Unknown'}
Description:
{description[:3000]}

Respond with a JSON object only — no prose, no markdown fences:
{{"score": <integer 0-100>, "reason": "<your assessment as described below>"}}

score: integer 0–100 (0 = no match, 100 = perfect match)
reason: {reason_instr}
"""


def _parse_result(raw: str) -> tuple[int, str] | None:
    """Extract {score, reason} from a model reply, tolerating markdown fences and
    surrounding prose (local models are chattier than Claude)."""
    if not raw:
        return None
    text = raw.strip()
    # Grab the first {...} object if there's extra text around it.
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]
    try:
        data = json.loads(text)
        score = max(0, min(100, int(data["score"])))
        reason = str(data["reason"])
        return score, reason
    except Exception:
        return None


def _call_claude(prompt: str, max_tokens: int = 256) -> str | None:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("[matcher] ANTHROPIC_API_KEY not set — skipping scoring.")
        return None
    from app_settings import get_claude_model
    model = get_claude_model()
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text.strip()


def _call_local(prompt: str, max_tokens: int = 256) -> str | None:
    """Call a local OpenAI-compatible chat endpoint (Ollama, LM Studio, llama.cpp,
    vLLM, …). The user configures base_url + model in the web UI."""
    from app_settings import get_local_llm
    import httpx
    cfg = get_local_llm()
    base = (cfg.get("base_url") or "").rstrip("/")
    model = cfg.get("model") or ""
    if not base or not model:
        print("[matcher] local LLM base_url/model not configured — skipping scoring.")
        return None
    url = f"{base}/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": max_tokens,
        # Ask servers that support it to emit strict JSON; ignored by those that don't.
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": "Bearer local", "Content-Type": "application/json"}
    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=120)
    except Exception:
        # Some servers reject response_format — retry once without it.
        payload.pop("response_format", None)
        resp = httpx.post(url, json=payload, headers=headers, timeout=120)
    resp.raise_for_status()
    data = resp.json()
    return (data["choices"][0]["message"]["content"] or "").strip()


def list_local_models(base_url: str) -> tuple[bool, list[str] | str]:
    """Query an OpenAI-compatible server's /models endpoint to discover which models
    are loaded/available. Returns (True, [model_ids]) or (False, error_message)."""
    import httpx
    base = (base_url or "").rstrip("/")
    if not base:
        return False, "Enter a Base URL first."
    try:
        r = httpx.get(f"{base}/models",
                      headers={"Authorization": "Bearer local"}, timeout=15)
        r.raise_for_status()
        data = r.json()
        ids = [m.get("id") for m in data.get("data", []) if m.get("id")]
        if not ids:
            return False, "Server returned no models."
        return True, ids
    except Exception as e:
        return False, f"Failed: {e}"


def test_local_connection(base_url: str, model: str) -> tuple[bool, str]:
    """Ping a local OpenAI-compatible endpoint with a trivial prompt so the user can
    verify their settings from the UI before relying on them for scoring."""
    import httpx
    base = (base_url or "").rstrip("/")
    if not base or not (model or "").strip():
        return False, "Enter both a Base URL and a Model name."
    try:
        resp = httpx.post(
            f"{base}/chat/completions",
            json={
                "model": model.strip(),
                "messages": [{"role": "user", "content": "Reply with the single word: OK"}],
                "max_tokens": 5,
                "temperature": 0,
            },
            headers={"Authorization": "Bearer local", "Content-Type": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        return True, f"Connected ✓ — model replied: {content.strip()[:40]!r}"
    except Exception as e:
        return False, f"Failed: {e}"


def score_job(job_id: int, description: str, title: str | None, company: str | None,
              backend: str | None = None, raise_errors: bool = False) -> tuple[int, str] | None:
    """
    Score the job against the resume. `backend` forces a specific engine
    ("claude"/"local"); when None, the configured default is used. Returns
    (score, reason) or None on failure. With raise_errors=True, backend/parse
    failures raise instead of returning None (so callers can show the reason).
    """
    resume = _get_resume()
    if not resume:
        if raise_errors:
            raise RuntimeError("resume.txt not found")
        return None

    from app_settings import get_scoring_backend, get_rationale_detail
    backend = backend or get_scoring_backend()
    detail = get_rationale_detail()
    prompt = _build_prompt(resume, description, title, company, detail)
    max_tokens = _RATIONALE_MAX_TOKENS.get(detail, 450)
    try:
        raw = (_call_local(prompt, max_tokens) if backend == "local"
               else _call_claude(prompt, max_tokens))
    except Exception as e:
        print(f"[matcher] {backend} error for job {job_id}: {e}")
        if raise_errors:
            raise
        return None

    result = _parse_result(raw) if raw else None
    if result is None:
        print(f"[matcher] could not parse {backend} reply for job {job_id}: {raw!r}")
        if raise_errors:
            raise RuntimeError("empty/unparseable model reply")
    return result


def score_job_if_enabled(job_id: int) -> str:
    """
    Called after upsert — scores only if enabled in config and job has a description.
    Returns a short status string for logging:
      'scored N/100' | 'already scored' | 'no description' | 'disabled' |
      'scoring OFF (global)' | 'error'
    """
    # Global runtime kill-switch (web UI checkbox) — master gate over config.
    # Lets the user scrape/test without spending Claude API money.
    try:
        from app_settings import get_scoring_enabled
        if not get_scoring_enabled():
            return "scoring OFF (global)"
    except Exception:
        pass  # if settings unavailable, fall back to config behavior

    from app_settings import get_auto_score_on_ingest, get_scoring_backend
    if not get_auto_score_on_ingest():
        return "disabled"

    from db.models import get_job, set_match
    backend = get_scoring_backend()
    score_col = "local_score" if backend == "local" else "match_score"

    job = get_job(job_id)
    if job is None:
        return "error"
    # "Already scored" is per-backend: a job scored by Claude can still be scored
    # by the local LLM (separate column) and vice versa.
    if job[score_col] is not None:
        return f"already scored {job[score_col]}/100"
    if not job["description"]:
        return "no description"

    result = score_job(job_id, job["description"], job["title"], job["company"], backend)
    if result:
        score, reason = result
        set_match(job_id, score, reason, backend)
        print(f"[matcher] job {job_id} scored {score}/100 ({backend})")
        return f"scored {score}/100"
    return "error"


def rescore_job(job_id: int, backend: str) -> str:
    """Force a re-score of a single job against a specific backend (ignores the
    'already scored' skip and the global on/off gate — used by the bulk Rescore
    action where the user has explicitly asked for it). Returns a status string."""
    from db.models import get_job, set_match
    job = get_job(job_id)
    if job is None:
        return "not found"
    if not job["description"]:
        return "no description"
    try:
        result = score_job(job_id, job["description"], job["title"], job["company"],
                           backend, raise_errors=True)
    except Exception as e:
        return f"error: {e}"
    if result:
        score, reason = result
        set_match(job_id, score, reason, backend)
        return f"scored {score}/100"
    return "error: no result"


def score_all_unscored() -> int:
    from db.models import get_unscored_jobs, set_match

    jobs = get_unscored_jobs()
    print(f"[matcher] {len(jobs)} unscored jobs")
    count = 0
    for job in jobs:
        result = score_job(job["id"], job["description"], job["title"], job["company"])
        if result:
            score, reason = result
            set_match(job["id"], score, reason)
            print(f"  job {job['id']} ({job['title']}) → {score}/100")
            count += 1
    return count


if __name__ == "__main__":
    if "--score-all" in sys.argv:
        n = score_all_unscored()
        print(f"Scored {n} jobs.")
    else:
        print("Usage: python -m matching.resume_matcher --score-all")
