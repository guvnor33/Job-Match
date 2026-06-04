"""
Shared helper: load and write search_terms.toml (user-editable search config).
Uses tomllib (stdlib, read-only) for loading and tomlkit for writing.
"""
import tomllib
from pathlib import Path

_ROOT = Path(__file__).parent.parent
_TOML_PATH = _ROOT / "search_terms.toml"
_cache: dict | None = None


def _invalidate_cache() -> None:
    global _cache
    _cache = None


def load_search_config() -> dict:
    global _cache
    if _cache is None:
        with open(_TOML_PATH, "rb") as f:
            _cache = tomllib.load(f)
    return _cache


# ---------------------------------------------------------------------------
# LinkedIn search terms (read/write)
# ---------------------------------------------------------------------------

def get_linkedin_terms() -> list[str]:
    return list(load_search_config().get("linkedin", {}).get("search_terms", []))


def add_linkedin_term(term: str) -> None:
    import tomlkit
    text = _TOML_PATH.read_text(encoding="utf-8")
    doc = tomlkit.parse(text)
    terms = doc["linkedin"]["search_terms"]
    if term not in terms:
        terms.append(term)
    _TOML_PATH.write_text(tomlkit.dumps(doc), encoding="utf-8")
    _invalidate_cache()


def remove_linkedin_term(term: str) -> None:
    import tomlkit
    text = _TOML_PATH.read_text(encoding="utf-8")
    doc = tomlkit.parse(text)
    terms = doc["linkedin"]["search_terms"]
    if term in terms:
        terms.remove(term)
    _TOML_PATH.write_text(tomlkit.dumps(doc), encoding="utf-8")
    _invalidate_cache()


def get_linkedin_defaults() -> dict:
    cfg = load_search_config().get("linkedin", {})
    return {
        "location":        cfg.get("location", ""),
        "remote_only":     cfg.get("remote_only", False),
        "lookback_hours":  24,
        "max_results":     cfg.get("max_results_per_term", 25),
    }


# ---------------------------------------------------------------------------
# Blacklist
# ---------------------------------------------------------------------------

def get_blacklist_patterns() -> list[str]:
    cfg = load_search_config()
    return [p.lower() for p in cfg.get("blacklist", {}).get("title_patterns", [])]


def is_blacklisted_title(title: str) -> bool:
    title_lower = title.lower()
    return any(pattern in title_lower for pattern in get_blacklist_patterns())


# ---------------------------------------------------------------------------
# Company blacklist — global, applies to ALL sources. Junk/scam companies.
# ---------------------------------------------------------------------------

def get_blacklist_companies() -> list[str]:
    """Raw company-blacklist entries as stored (original casing), sorted."""
    cfg = load_search_config()
    return sorted(cfg.get("blacklist", {}).get("companies", []), key=str.lower)


def is_blacklisted_company(company: str | None) -> bool:
    """Case-insensitive substring match. A blacklisted job is dropped BEFORE it
    is stored or sent to the AI scorer (protects the API budget)."""
    if not company:
        return False
    company_lower = company.lower()
    return any(
        entry.lower() in company_lower
        for entry in get_blacklist_companies()
    )


def add_blacklist_company(name: str) -> None:
    import tomlkit
    name = name.strip()
    if not name:
        return
    doc = tomlkit.parse(_TOML_PATH.read_text(encoding="utf-8"))
    bl = doc.setdefault("blacklist", tomlkit.table())
    companies = bl.get("companies")
    if companies is None:
        companies = tomlkit.array()
        companies.multiline(True)
        bl["companies"] = companies
    # case-insensitive dedupe
    if not any(c.lower() == name.lower() for c in companies):
        companies.append(name)
    _TOML_PATH.write_text(tomlkit.dumps(doc), encoding="utf-8")
    _invalidate_cache()


def remove_blacklist_company(name: str) -> None:
    import tomlkit
    doc = tomlkit.parse(_TOML_PATH.read_text(encoding="utf-8"))
    companies = doc.get("blacklist", {}).get("companies")
    if not companies:
        return
    for c in list(companies):
        if c.lower() == name.lower():
            companies.remove(c)
    _TOML_PATH.write_text(tomlkit.dumps(doc), encoding="utf-8")
    _invalidate_cache()
