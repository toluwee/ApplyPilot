"""Deterministic rubric-based job screening.

Remote-only, no contract/freelance/hourly, senior/lead seniority, minimum
annual salary. All checks here are rule-based (keyword + regex + structured
fields) -- no LLM calls -- because these criteria are objective facts, not
judgment calls, and enforcing them with a probabilistic prompt instruction
(as scoring/scorer.py's SCORE_PROMPT alone previously did) lets bad matches
through. This module is called twice: once at discovery time (before a job
is stored, with whatever fields the source gave us) and again after
enrichment (once full_description is available, catching disqualifiers the
discovery-time fields missed).
"""

import logging
import re

log = logging.getLogger(__name__)


# ── Config ────────────────────────────────────────────────────────────────

_DEFAULT_SENIORITY_KEYWORDS = ["senior", "sr.", "sr ", "staff", "lead", "principal"]

_REMOTE_KEYWORDS = ("remote", "anywhere", "work from home", "wfh", "distributed", "virtual")

_CONTRACT_KEYWORDS = (
    "contract", "contractor", "freelance", "c2c", "corp-to-corp", "corp to corp",
    "1099", "temp ", "temporary", "seasonal", "gig",
)

_HOURLY_KEYWORDS = ("/hr", "/ hr", "/hour", "/ hour", "per hour", "hourly")

_JOBSPY_CONTRACT_TYPES = {"contract", "internship", "temporary", "perdiem", "per_diem", "seasonal"}

_ONSITE_TITLE_OVERRIDE_KEYWORDS = ("on-site", "onsite", "on site", "hybrid", "in-office", "in office")


def load_rubric_config(search_cfg: dict | None = None, profile: dict | None = None) -> dict:
    """Load rubric settings from searches.yaml, falling back to the user's
    profile.json for the minimum salary when not set explicitly.

    Also reads the location accept/reject patterns (nested under `location:`
    in searches.yaml) and exclude_titles, so callers only need this one dict.
    """
    from applypilot import config as _config

    if search_cfg is None:
        search_cfg = _config.load_search_config()

    rubric_cfg = search_cfg.get("rubric", {}) or {}

    min_salary = rubric_cfg.get("min_salary_usd")
    if min_salary is None:
        if profile is None:
            try:
                profile = _config.load_profile()
            except FileNotFoundError:
                profile = {}
        comp = (profile or {}).get("compensation", {})
        raw = comp.get("salary_range_min")
        if raw:
            try:
                min_salary = float(str(raw).replace(",", "").replace("$", ""))
            except ValueError:
                min_salary = None

    location_cfg = search_cfg.get("location", {}) or {}

    return {
        "remote_only": rubric_cfg.get("remote_only", True),
        "exclude_contract": rubric_cfg.get("exclude_contract", True),
        "exclude_hourly": rubric_cfg.get("exclude_hourly", True),
        "min_salary_usd": min_salary,
        "seniority_keywords": [k.lower() for k in rubric_cfg.get("seniority_keywords", _DEFAULT_SENIORITY_KEYWORDS)],
        "exclude_titles": [t.lower() for t in search_cfg.get("exclude_titles", [])],
        "location_accept": location_cfg.get("accept_patterns", []),
        "location_reject": location_cfg.get("reject_patterns", []),
    }


# ── Location / remote check ──────────────────────────────────────────────

def _remote_signal(
    location: str | None, is_remote: bool | None, remote_type: str | None, title: str | None = None,
) -> bool | None:
    """Determine remote status from structured signals or explicit keywords only.

    Deliberately does NOT consult the accept/reject region list -- that list
    answers "is this an acceptable region" (useful for onsite-acceptable
    searches), not "is this remote". A location like "US FL JAX 347" or
    "USA, CA, Pleasanton" matches "US"/"USA" in a typical accept list despite
    being a physical office address; treating that as proof of remote work
    was the discovery-time remote_only gate letting non-remote jobs through.

    Signal priority: structured API signal, then an explicit on-site/hybrid
    statement in the title, then a remote keyword in the location text.
    The title check exists because location text can be misleading on its
    own -- confirmed live on a Workday posting titled "...On-site: Scott Air
    Force Base, IL" whose location field was just "Remote-US" (an internal
    regional-pool label, not a remote-work claim). The title is a more
    specific, human-authored statement, so it vetoes a text-only location
    match before that weaker check even runs.

    Returns True/False when a signal is present, None when genuinely unknown.
    """
    if remote_type:
        rt = remote_type.lower()
        if "hybrid" in rt or "on-site" in rt or "onsite" in rt or "on site" in rt:
            return False
        if "remote" in rt:
            return True

    if is_remote:
        return True

    if title and any(k in title.lower() for k in _ONSITE_TITLE_OVERRIDE_KEYWORDS):
        return False

    if location and any(r in location.lower() for r in _REMOTE_KEYWORDS):
        return True

    return None


def location_ok(
    location: str | None,
    accept: list[str],
    reject: list[str],
    is_remote: bool | None = None,
    remote_type: str | None = None,
) -> bool:
    """Check whether a job's location/remote signals pass the accept/reject filter.

    Structured signals and explicit remote keywords take priority (via
    _remote_signal); falls back to accept/reject substring matching on the
    location text otherwise. Unknown location is treated as neutral (kept).

    This is a broader "is this an acceptable region" check than a strict
    remote determination -- used as a coarse, cost-saving pre-filter (e.g.
    workday.py skips fetching full job details for obviously-rejected
    regions) before the authoritative remote_only gate in evaluate_rubric,
    which uses _remote_signal directly and does not consult accept/reject.
    """
    signal = _remote_signal(location, is_remote, remote_type)
    if signal is not None:
        return signal

    if not location:
        return True

    loc = location.lower()

    for r in reject:
        if r.lower() in loc:
            return False

    return any(a.lower() in loc for a in accept)


# ── Salary extraction ────────────────────────────────────────────────────

_INTERVAL_MULTIPLIERS = {
    "hourly": 2080,
    "daily": 260,
    "weekly": 52,
    "monthly": 12,
    "yearly": 1,
    "annual": 1,
}


def _amount_pattern(prefix: str) -> str:
    return rf"\$\s?(?P<{prefix}>\d[\d,]*\.?\d*)\s?(?P<{prefix}k>k)?"


_RANGE_TAIL = r"(?:\s*(?:[-–—]|to)\s*" + _amount_pattern("hi") + r")?"
_UNIT = r"\s*(?P<unit>/\s?hr|/\s?hour|per\s+hour|/\s?yr|/\s?year|annually)?"

_SALARY_CONTEXT_RE = re.compile(
    r"(?:salary|compensation|pay\s*range|base\s*pay|total\s*comp|annual\s*pay)[^\n$]{0,80}?"
    + _amount_pattern("lo") + _RANGE_TAIL + _UNIT,
    re.IGNORECASE,
)
_BARE_SALARY_RE = re.compile(_amount_pattern("lo") + _RANGE_TAIL + _UNIT, re.IGNORECASE)


def _to_float(v) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # filter NaN (pandas rows use float NaN for missing)


def _parse_amount(raw: str | None, k_suffix: str | None) -> float | None:
    if not raw:
        return None
    val = float(raw.replace(",", ""))
    if k_suffix:
        val *= 1000
    return val


def _plausible_annual(v: float) -> bool:
    return 20_000 <= v <= 1_000_000


def _plausible_hourly(v: float) -> bool:
    return 10 <= v <= 500


def _extract_salary_from_text(text: str) -> tuple[float | None, float | None, str]:
    """Regex-scan free text for a dollar figure, preferring matches near an
    explicit salary/compensation keyword to avoid picking up unrelated dollar
    amounts (e.g. "$2M in cost savings" in a job description bullet)."""
    match = _SALARY_CONTEXT_RE.search(text) or _BARE_SALARY_RE.search(text)
    if not match:
        return None, None, "unknown"

    lo = _parse_amount(match.group("lo"), match.group("lok"))
    hi = _parse_amount(match.group("hi"), match.group("hik"))
    unit = (match.group("unit") or "").lower()
    hourly = "hr" in unit or "hour" in unit

    if lo is None and hi is None:
        return None, None, "unknown"

    if hourly:
        if lo is not None and not _plausible_hourly(lo):
            return None, None, "unknown"
        if hi is not None and not _plausible_hourly(hi):
            hi = None
        lo = lo * 2080 if lo is not None else None
        hi = hi * 2080 if hi is not None else None
    else:
        if lo is not None and not _plausible_annual(lo):
            return None, None, "unknown"
        if hi is not None and not _plausible_annual(hi):
            hi = None

    return lo, hi, "listed"


def _infer_non_us_currency(location: str | None) -> str | None:
    """Guess a non-USD currency from location text, or None if it looks US /
    is ambiguous.

    Workday's own location format is "City, Region, CCC" (e.g. confirmed
    live: "Toronto, ON, CAN", "USA, CA, Pleasanton") -- a trailing ISO
    country code -- plus plain-text fallbacks for other sources. Used so a
    free-text salary figure (no structured currency field, e.g. every
    Workday posting) isn't compared to a USD threshold as if it were USD:
    a CAD salary in the same raw digit range as a USD one is worth roughly
    25-30% less, which can flip a pass/fail either direction.
    """
    if not location:
        return None
    loc = location.lower()
    if "usa" in loc or "united states" in loc:
        return None
    if "canada" in loc or re.search(r"\bcan\b", loc):
        return "CAD"
    if "united kingdom" in loc or re.search(r"\bgbr\b", loc) or re.search(r"\buk\b", loc):
        return "GBP"
    if "india" in loc or re.search(r"\bind\b", loc):
        return "INR"
    return None


_COUNTRY_CURRENCY = {"CA": "CAD", "GB": "GBP", "IN": "INR"}


def _currency_from_country_code(code: str | None) -> str | None:
    """Map an ISO alpha-2 country code (e.g. Workday's jobRequisitionLocation
    .country.alpha2Code, confirmed live to be reliably populated) to a
    currency override for extract_salary_annual.

    None means "no override" (unknown code, or US). Any non-US code without
    a specific mapping still returns a non-USD marker so the existing
    currency guard treats it as unknown rather than comparing a foreign
    salary figure to the USD threshold -- this is a stronger signal than
    the free-text _infer_non_us_currency guess below, so callers should
    prefer it when a country_code is available.
    """
    if not code or code.strip().upper() == "US":
        return None
    return _COUNTRY_CURRENCY.get(code.strip().upper(), "XXX")


def extract_salary_annual(
    min_amt=None,
    max_amt=None,
    interval: str | None = None,
    currency: str | None = None,
    text_fallback: str | None = None,
) -> tuple[float | None, float | None, str]:
    """Normalize salary to an annual USD figure.

    Prefers structured min/max/interval fields (from JobSpy) when present.
    Falls back to regex-scanning free text (salary string or description)
    when they aren't. Non-USD currencies are treated as unknown rather than
    guessed at, since a wrong FX conversion could wrongly pass or fail a job.

    Returns (min_annual, max_annual, "listed"|"unknown").
    """
    if currency and currency.strip().upper() not in ("", "USD", "$"):
        return None, None, "unknown"

    lo = _to_float(min_amt)
    hi = _to_float(max_amt)

    if lo is not None or hi is not None:
        mult = _INTERVAL_MULTIPLIERS.get((interval or "yearly").lower(), 1)
        lo_annual = lo * mult if lo is not None else None
        hi_annual = hi * mult if hi is not None else None
        return lo_annual, hi_annual, "listed"

    if not text_fallback:
        return None, None, "unknown"

    return _extract_salary_from_text(text_fallback)


# ── Full rubric evaluation ───────────────────────────────────────────────

def evaluate_rubric(job: dict, cfg: dict) -> dict:
    """Evaluate a job dict against the rubric.

    `job` may include any of: title, location, salary, description,
    full_description, is_remote, remote_type, job_type, min_amount,
    max_amount, interval, currency. Missing fields are treated as unknown,
    never as a failure by themselves (e.g. no salary listed never fails the
    salary check).

    Returns {"passed": bool, "reasons": list[str], "salary_confidence": "listed"|"unknown"}.
    """
    reasons: list[str] = []
    title = (job.get("title") or "").lower()
    text_blob = " ".join(
        str(job.get(k) or "") for k in ("title", "salary", "description", "full_description")
    ).lower()

    remote_signal = _remote_signal(
        job.get("location"), job.get("is_remote"), job.get("remote_type"), title=job.get("title"),
    )
    if cfg["remote_only"] and remote_signal is not True:
        reasons.append("not-remote")

    job_type = (job.get("job_type") or "").lower()
    if cfg["exclude_contract"]:
        if job_type and any(t in job_type for t in _JOBSPY_CONTRACT_TYPES):
            reasons.append("contract-job-type")
        elif any(k in text_blob for k in _CONTRACT_KEYWORDS):
            reasons.append("contract-keyword")

    interval = (job.get("interval") or "").lower()
    if cfg["exclude_hourly"]:
        if interval == "hourly":
            reasons.append("hourly-interval")
        elif any(k in text_blob for k in _HOURLY_KEYWORDS):
            reasons.append("hourly-keyword")

    if cfg["seniority_keywords"] and not any(k in title for k in cfg["seniority_keywords"]):
        reasons.append("under-seniority")

    matched_excl = next((t for t in cfg["exclude_titles"] if t in title), None)
    if matched_excl:
        reasons.append(f"excluded-title:{matched_excl}")

    currency = (
        job.get("currency")
        or _currency_from_country_code(job.get("country_code"))
        or _infer_non_us_currency(job.get("location"))
    )
    min_annual, max_annual, salary_confidence = extract_salary_annual(
        job.get("min_amount"), job.get("max_amount"), job.get("interval"), currency,
        text_fallback=job.get("salary") or job.get("full_description") or job.get("description"),
    )
    min_salary_usd = cfg.get("min_salary_usd")
    if min_salary_usd and salary_confidence == "listed":
        best = max_annual if max_annual is not None else min_annual
        if best is not None and best < min_salary_usd:
            reasons.append(f"under-salary:{int(best)}")

    return {"passed": len(reasons) == 0, "reasons": reasons, "salary_confidence": salary_confidence}


def summarize_rejections(all_reasons: list[list[str]]) -> dict[str, int]:
    """Tally per-job rejection reason lists into a small set of buckets for logging."""
    buckets = {"not-remote": 0, "contract/hourly": 0, "under-seniority": 0, "under-salary": 0}
    for reasons in all_reasons:
        if "not-remote" in reasons:
            buckets["not-remote"] += 1
        if any(r.startswith(("contract-", "hourly-")) for r in reasons):
            buckets["contract/hourly"] += 1
        if any(r == "under-seniority" or r.startswith("excluded-title:") for r in reasons):
            buckets["under-seniority"] += 1
        if any(r.startswith("under-salary:") for r in reasons):
            buckets["under-salary"] += 1
    return buckets
