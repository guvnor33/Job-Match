"""
Persisted application settings, stored in settings.toml (read with tomllib, written
with tomlkit — same convention as search_terms.toml). Covers the scoring kill-switch,
the scoring backend (Claude API vs. a local OpenAI-compatible LLM), the local endpoint,
and the rationale detail level.

These are runtime toggles flipped from the web UI (not static config), but the user
asked for everything to live in TOML — so this is the single settings file for the app.
Legacy app_settings.json is migrated automatically on first load.
"""
import threading
import tomllib
from pathlib import Path

_DIR = Path(__file__).parent
_TOML = _DIR / "settings.toml"
_JSON_LEGACY = _DIR / "app_settings.json"
_lock = threading.Lock()

_DEFAULTS = {
    "scoring_enabled":      True,                          # master on/off switch
    "auto_score_on_ingest": True,                          # score new jobs during a scrape
    "scoring_backend":      "claude",                      # "claude" | "local"
    "claude_model":         "claude-opus-4-5",             # model used for the Claude backend
    "rationale_detail":     "detailed",                    # "brief" | "standard" | "detailed"
    "local_base_url":       "http://localhost:11434/v1",   # Ollama default; LM Studio = :1234/v1
    "local_model":          "llama3.1",
}

# Order the keys are written to the [scoring] table.
_KEYS = ("scoring_enabled", "auto_score_on_ingest", "scoring_backend", "claude_model",
         "rationale_detail", "local_base_url", "local_model")


def _read_toml() -> dict:
    if not _TOML.exists():
        return {}
    try:
        with open(_TOML, "rb") as f:
            return tomllib.load(f).get("scoring", {}) or {}
    except Exception:
        return {}


def _migrate_legacy() -> dict | None:
    """One-time seed for a fresh settings.toml: pull the Claude model + auto-score flag
    from config.yaml, and the scoring/local-LLM choices from the old app_settings.json
    (dropping its empty local fields, which an earlier bug had wiped to '')."""
    seed: dict = {}
    # config.yaml → claude_model, auto_score_on_ingest
    try:
        import yaml
        cfg = yaml.safe_load((_DIR / "config.yaml").read_text()) or {}
        m = cfg.get("matching", {})
        if m.get("model"):
            seed["claude_model"] = m["model"]
        if "auto_score_on_ingest" in m:
            seed["auto_score_on_ingest"] = bool(m["auto_score_on_ingest"])
    except Exception:
        pass
    # legacy app_settings.json → scoring toggles + local LLM
    if _JSON_LEGACY.exists():
        import json
        try:
            with open(_JSON_LEGACY, encoding="utf-8") as f:
                old = json.load(f)
            if not (old.get("local_base_url") or "").strip():
                old.pop("local_base_url", None)
            if not (old.get("local_model") or "").strip():
                old.pop("local_model", None)
            seed.update(old)
        except Exception:
            pass
    return seed or None


def _write(settings: dict) -> None:
    import tomlkit
    doc = tomlkit.document()
    doc.add(tomlkit.comment(" JobScrape application settings — managed by the web UI."))
    table = tomlkit.table()
    for k in _KEYS:
        table[k] = settings.get(k, _DEFAULTS[k])
    doc["scoring"] = table
    with open(_TOML, "w", encoding="utf-8") as f:
        f.write(tomlkit.dumps(doc))


def _load() -> dict:
    data = _read_toml()
    if not data:
        migrated = _migrate_legacy()
        if migrated is not None:
            merged = {**_DEFAULTS, **migrated}
            _write(merged)
            try:
                _JSON_LEGACY.unlink()   # remove legacy file once migrated
            except Exception:
                pass
            return merged
    return {**_DEFAULTS, **data}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_settings() -> dict:
    """All settings as a flat dict (with defaults filled in) for rendering the UI."""
    return _load()


def get_scoring_enabled() -> bool:
    return bool(_load().get("scoring_enabled", True))


def set_scoring_enabled(value: bool) -> bool:
    with _lock:
        data = _load()
        data["scoring_enabled"] = bool(value)
        _write(data)
    return bool(value)


def get_scoring_backend() -> str:
    backend = _load().get("scoring_backend", "claude")
    return backend if backend in ("claude", "local") else "claude"


def get_auto_score_on_ingest() -> bool:
    return bool(_load().get("auto_score_on_ingest", True))


def get_claude_model() -> str:
    return _load().get("claude_model") or _DEFAULTS["claude_model"]


def get_local_llm() -> dict:
    d = _load()
    # Fall back to defaults if somehow blank, so scoring never silently no-ops.
    base = (d.get("local_base_url") or "").strip() or _DEFAULTS["local_base_url"]
    model = (d.get("local_model") or "").strip() or _DEFAULTS["local_model"]
    return {"base_url": base, "model": model}


def get_rationale_detail() -> str:
    d = _load().get("rationale_detail", "detailed")
    return d if d in ("brief", "standard", "detailed") else "detailed"


def set_rationale_detail(value: str) -> str:
    with _lock:
        data = _load()
        data["rationale_detail"] = value if value in ("brief", "standard", "detailed") else "detailed"
        _write(data)
    return data["rationale_detail"]


def set_scoring_backend(backend: str, base_url: str | None = None,
                        model: str | None = None) -> dict:
    """Persist the backend choice and (for local) its endpoint + model. Empty
    base_url/model are IGNORED so a stray blank submit can't wipe a working config."""
    with _lock:
        data = _load()
        data["scoring_backend"] = backend if backend in ("claude", "local") else "claude"
        if base_url is not None and base_url.strip():
            data["local_base_url"] = base_url.strip()
        if model is not None and model.strip():
            data["local_model"] = model.strip()
        _write(data)
    return data
