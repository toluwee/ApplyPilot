"""ApplyPilot HTML Dashboard Generator + Local Review Server.

Generates a self-contained HTML dashboard with:
  - Review queue: approve/reject scored jobs before tailoring
  - Ready to apply: approved jobs with their generated resume + cover letter
  - Summary stats (total, enriched, scored, high-fit)
  - Score distribution bar chart
  - Jobs-by-source breakdown
  - Filterable job cards grouped by score
  - Client-side search and score filtering
"""

from __future__ import annotations

import json
import logging
import threading
import webbrowser
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import quote, urlparse

from rich.console import Console

from applypilot.config import APP_DIR
from applypilot.database import get_connection

console = Console()
log = logging.getLogger(__name__)

# Why the auto-apply bot left a job for the human to finish.
_STRANDED_REASONS = {
    "in_progress": "Auto-apply interrupted",
    "manual": "ATS requires manual apply",
}


def _doc_links(job_url: str, txt_path: str | None, kind: str, live: bool) -> str:
    """Render the links for one generated document (resume or cover letter).

    PDFs have no database column — convert_to_pdf() writes them next to the
    source .txt at the same stem, so the path is re-derived here the same way
    apply/prompt.py does. PDF generation is best-effort, so a missing PDF
    degrades to the .txt rather than a broken link.

    In served mode the files go through /api/file. In the static snapshot the
    page's own origin is already file://, so same-scheme file:// links work
    (a served page could not link to them — browsers block that downgrade).
    """
    if not txt_path:
        return '<span class="doc-missing">not generated</span>'

    txt = Path(txt_path)
    pdf = txt.with_suffix(".pdf")
    has_txt, has_pdf = txt.is_file(), pdf.is_file()

    if not has_txt and not has_pdf:
        return '<span class="doc-missing">file missing from disk</span>'

    def href(path: Path, fmt: str) -> str:
        if not live:
            return escape(path.resolve().as_uri())
        return f"/api/file?url={quote(job_url, safe='')}&amp;kind={kind}&amp;fmt={fmt}"

    parts = []
    if has_pdf:
        parts.append(f'<a class="doc-link" href="{href(pdf, "pdf")}" target="_blank">PDF</a>')
    if has_txt:
        parts.append(f'<a class="doc-link-alt" href="{href(txt, "txt")}" target="_blank">txt</a>')

    if not has_pdf:
        # `applypilot run pdf` only scans TAILORED_DIR (scoring/pdf.py
        # batch_convert), so it can never produce a cover-letter PDF —
        # don't suggest a command that won't help.
        hint = ("no PDF — run <code>applypilot run pdf</code>" if kind == "resume"
                else "text only")
        parts.append(f'<span class="doc-missing">{hint}</span>')
    return " ".join(parts)


def _build_html(conn, live: bool = True) -> str:
    """Build the full dashboard HTML string from the current DB state.

    Args:
        conn: Database connection to read from.
        live: True when served by serve_dashboard(), so the /api/* routes
            exist. False for the static snapshot, where document links and
            the Mark Applied button are rendered inert instead of broken.
    """
    # get_pending_review/set_approval are re-exported for the serve path
    from applypilot.database import (  # noqa: F401
        get_pending_review,
        get_ready_to_apply,
        set_approval,
    )

    # Stats
    total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    scored = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE fit_score IS NOT NULL"
    ).fetchone()[0]
    high_fit = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE fit_score >= 7"
    ).fetchone()[0]
    pending_review = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE fit_score >= 7 AND full_description IS NOT NULL "
        "AND approval_status IS NULL AND applied_at IS NULL"
    ).fetchone()[0]
    approved_count = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE approval_status = 'approved'"
    ).fetchone()[0]
    rejected_count = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE approval_status = 'rejected'"
    ).fetchone()[0]

    # Pending review jobs (for the review queue section)
    review_rows = conn.execute("""
        SELECT url, title, site, location, fit_score, score_reasoning, full_description, application_url
        FROM jobs
        WHERE fit_score >= 7
          AND full_description IS NOT NULL
          AND approval_status IS NULL
          AND applied_at IS NULL
        ORDER BY fit_score DESC, discovered_at DESC
    """).fetchall()
    review_jobs = [dict(zip(r.keys(), r, strict=True)) for r in review_rows] if review_rows else []

    # Approved + tailored jobs still awaiting a manual application
    ready_jobs = get_ready_to_apply(conn)
    ready_count = len(ready_jobs)

    # Score distribution
    score_dist: dict[int, int] = {}
    if scored:
        rows = conn.execute(
            "SELECT fit_score, COUNT(*) FROM jobs "
            "WHERE fit_score IS NOT NULL "
            "GROUP BY fit_score ORDER BY fit_score DESC"
        ).fetchall()
        for r in rows:
            score_dist[r[0]] = r[1]

    # Site stats
    site_stats = conn.execute("""
        SELECT site,
               COUNT(*) as total,
               SUM(CASE WHEN fit_score >= 7 THEN 1 ELSE 0 END) as high_fit,
               SUM(CASE WHEN fit_score BETWEEN 5 AND 6 THEN 1 ELSE 0 END) as mid_fit,
               SUM(CASE WHEN fit_score < 5 AND fit_score IS NOT NULL THEN 1 ELSE 0 END) as low_fit,
               SUM(CASE WHEN fit_score IS NULL THEN 1 ELSE 0 END) as unscored,
               ROUND(AVG(fit_score), 1) as avg_score
        FROM jobs GROUP BY site ORDER BY high_fit DESC, total DESC
    """).fetchall()

    # All scored jobs (5+), ordered by score desc
    jobs = conn.execute("""
        SELECT url, title, salary, description, location, site, strategy,
               full_description, application_url, detail_error,
               fit_score, score_reasoning
        FROM jobs
        WHERE fit_score >= 5
        ORDER BY fit_score DESC, site, title
    """).fetchall()

    # Color map per site
    colors = {
        "RemoteOK": "#10b981", "WelcomeToTheJungle": "#f59e0b",
        "Job Bank Canada": "#3b82f6", "CareerJet Canada": "#8b5cf6",
        "Hacker News Jobs": "#ff6600", "BuiltIn Remote": "#ec4899",
        "TD Bank": "#00a651", "CIBC": "#c41f3e", "RBC": "#003168",
        "indeed": "#2164f3", "linkedin": "#0a66c2",
        "Dice": "#eb1c26", "Glassdoor": "#0caa41",
    }

    # --- Review queue cards ---
    review_cards = []
    for j in review_jobs:
        score = j.get("fit_score", 0) or 0
        title = escape(j.get("title") or "Untitled")
        url_raw = j.get("url") or ""
        url_esc = escape(url_raw)
        site = escape(j.get("site") or "")
        location = escape((j.get("location") or "")[:40])
        site_color = colors.get(j.get("site") or "", "#6b7280")
        score_color = "#10b981" if score >= 9 else ("#34d399" if score >= 8 else ("#60a5fa" if score >= 7 else "#f59e0b"))

        reasoning_raw = j.get("score_reasoning") or ""
        reasoning_lines = reasoning_raw.split("\n")
        keywords = escape(reasoning_lines[0][:150]) if reasoning_lines else ""
        reasoning_body = escape(reasoning_lines[1][:250]) if len(reasoning_lines) > 1 else ""

        desc_len = len(j.get("full_description") or "")
        apply_url_esc = escape(j.get("application_url") or "")

        # URL is stored in data-url only; buttons use this.closest() — no URL in onclick
        # Full description is lazy-loaded via /api/description on <details> open
        review_cards.append(f"""
        <div class="rq-card" data-url="{url_esc}">
          <div class="rq-header">
            <span class="score-pill" style="background:{score_color}">{score}</span>
            <a href="{url_esc}" class="job-title" target="_blank">{title}</a>
            <div style="margin-left:auto;display:flex;gap:0.4rem;flex-shrink:0">
              <button class="btn-approve" onclick="submitReviewCard(this,'approved')">&#10003; Approve</button>
              <button class="btn-reject" onclick="showRejectFormCard(this)">&#10007; Reject</button>
            </div>
          </div>
          <div class="meta-row">
            <span class="meta-tag site-tag" style="background:{site_color}33;color:{site_color}">{site}</span>
            {f'<span class="meta-tag location">{location}</span>' if location else ''}
            {f'<a href="{apply_url_esc}" class="meta-tag apply-tag" target="_blank">Open App</a>' if apply_url_esc else ''}
          </div>
          {f'<div class="keywords-row">{keywords}</div>' if keywords else ''}
          {f'<div class="reasoning-row">{reasoning_body}</div>' if reasoning_body else ''}
          <details class="full-desc-details" data-desc-url="{url_esc}">
            <summary class="expand-btn">Job Description ({desc_len:,} chars)</summary>
            <div class="full-desc"></div>
          </details>
          <div class="reject-form" style="display:none">
            <input class="note-input" placeholder="Reason (optional)" />
            <button class="btn-reject-confirm" onclick="submitRejectWithNoteCard(this)">Confirm Reject</button>
            <button class="btn-cancel" onclick="hideRejectFormCard(this)">Cancel</button>
          </div>
        </div>""")

    if review_jobs:
        review_queue_html = f"""
<div class="review-section" id="review-section">
  <h2 class="review-heading">
    Review Queue
    <span class="rq-badge" id="rq-badge">{len(review_jobs)}</span>
    <span style="font-size:0.75rem;font-weight:400;color:#94a3b8;margin-left:0.5rem">Approve or reject before tailoring</span>
  </h2>
  <div class="rq-grid" id="rq-grid">
    {"".join(review_cards)}
  </div>
</div>"""
    else:
        review_queue_html = """
<div class="review-section review-empty">
  <h2 class="review-heading">Review Queue <span class="rq-badge" style="background:#334155">0</span></h2>
  <p style="color:#64748b;font-size:0.9rem">No jobs pending review. Run <code>applypilot run score</code> to score new jobs.</p>
</div>"""

    # --- Ready to apply cards ---
    ready_cards = []
    for j in ready_jobs:
        score = j.get("fit_score", 0) or 0
        title = escape(j.get("title") or "Untitled")
        url_raw = j.get("url") or ""
        url_esc = escape(url_raw)
        site = escape(j.get("site") or "")
        location = escape((j.get("location") or "")[:40])
        salary = escape(j.get("salary") or "")
        site_color = colors.get(j.get("site") or "", "#6b7280")
        score_color = "#10b981" if score >= 9 else ("#34d399" if score >= 8 else "#60a5fa")

        # Prefer the direct application URL; fall back to the listing itself
        post_url = escape(j.get("application_url") or url_raw)

        resume_html = _doc_links(url_raw, j.get("tailored_resume_path"), "resume", live)
        cover_html = _doc_links(url_raw, j.get("cover_letter_path"), "cover", live)

        status = j.get("apply_status")
        reason = _STRANDED_REASONS.get(status or "")
        if status == "failed":
            reason = f"Auto-apply failed: {(j.get('apply_error') or 'unknown')[:60]}"
        stranded_html = f'<div class="stranded-note">{escape(reason)}</div>' if reason else ""

        btn_html = (
            '<button class="btn-applied" onclick="markApplied(this)">&#10003; Mark as Applied</button>'
            if live else
            '<button class="btn-applied" disabled title="Start the dashboard server to use this">&#10003; Mark as Applied</button>'
        )

        ready_cards.append(f"""
        <div class="ready-card" data-url="{url_esc}">
          <div class="rq-header">
            <span class="score-pill" style="background:{score_color}">{score}</span>
            <a href="{post_url}" class="job-title" target="_blank">{title}</a>
            <div style="margin-left:auto;flex-shrink:0">{btn_html}</div>
          </div>
          <div class="meta-row">
            <span class="meta-tag site-tag" style="background:{site_color}33;color:{site_color}">{site}</span>
            {f'<span class="meta-tag salary">{salary}</span>' if salary else ''}
            {f'<span class="meta-tag location">{location}</span>' if location else ''}
          </div>
          {stranded_html}
          <div class="doc-row"><span class="doc-label">Resume</span>{resume_html}</div>
          <div class="doc-row"><span class="doc-label">Cover</span>{cover_html}</div>
          <div class="card-footer">
            <a href="{post_url}" class="apply-link" target="_blank">Open job posting &rarr;</a>
          </div>
        </div>""")

    static_note = (
        '<p class="static-note">Static snapshot &mdash; documents open from disk, but '
        'run <code>applypilot dashboard</code> to mark jobs as applied.</p>'
        if not live else ""
    )

    if ready_jobs:
        ready_html = f"""
<div class="ready-section" id="ready-section">
  <h2 class="ready-heading">
    Ready to Apply
    <span class="ready-badge" id="ready-badge">{ready_count}</span>
    <span style="font-size:0.75rem;font-weight:400;color:#94a3b8;margin-left:0.5rem">Open each posting and apply with the generated documents</span>
  </h2>
  {static_note}
  <div class="ready-grid" id="ready-grid">
    {"".join(ready_cards)}
  </div>
</div>"""
    else:
        ready_html = """
<div class="ready-section review-empty">
  <h2 class="ready-heading">Ready to Apply <span class="ready-badge" style="background:#334155">0</span></h2>
  <p style="color:#64748b;font-size:0.9rem">Nothing ready yet. Approve jobs above, then run <code>applypilot run tailor cover pdf</code>.</p>
</div>"""

    # --- Score distribution bar chart ---
    score_bars = []
    max_count = max(score_dist.values()) if score_dist else 1
    for s in range(10, 0, -1):
        count = score_dist.get(s, 0)
        pct = (count / max_count * 100) if max_count else 0
        score_color_bar = "#10b981" if s >= 7 else ("#f59e0b" if s >= 5 else "#ef4444")
        score_bars.append(f"""
        <div class="score-row">
          <span class="score-label">{s}</span>
          <div class="score-bar-track">
            <div class="score-bar-fill" style="width:{pct}%;background:{score_color_bar}"></div>
          </div>
          <span class="score-count">{count}</span>
        </div>""")
    score_bars_html = "".join(score_bars)

    # --- Site stats rows ---
    site_rows = []
    for s in site_stats:
        site = s["site"] or "?"
        color = colors.get(site, "#6b7280")
        avg = s["avg_score"] or 0
        site_rows.append(f"""
        <div class="site-row">
          <div class="site-name" style="color:{color}">{escape(site)}</div>
          <div class="site-nums">{s['total']} jobs &middot; {s['high_fit']} strong fit &middot; avg score {avg}</div>
          <div class="bar-track">
            <div class="bar-fill" style="width:{s['high_fit']/max(s['total'],1)*100}%;background:{color}"></div>
            <div class="bar-fill" style="width:{s['mid_fit']/max(s['total'],1)*100}%;background:{color}66"></div>
          </div>
        </div>""")
    site_rows_html = "".join(site_rows)

    # --- Job cards grouped by score ---
    # Full descriptions are lazy-loaded on <details> open — not pre-rendered
    job_sections = []
    current_score = None
    for j in jobs:
        score = j["fit_score"] or 0
        if score != current_score:
            if current_score is not None:
                job_sections.append("</div>")
            score_color_jc = "#10b981" if score >= 7 else "#f59e0b"
            score_label = {
                10: "Perfect Match", 9: "Excellent Fit", 8: "Strong Fit",
                7: "Good Fit", 6: "Moderate+", 5: "Moderate",
            }.get(score, f"Score {score}")
            count_at_score = score_dist.get(score, 0)
            job_sections.append(f"""
            <h2 class="score-header" style="border-color:{score_color_jc}">
              <span class="score-badge" style="background:{score_color_jc}">{score}</span>
              {score_label} ({count_at_score} jobs)
            </h2>
            <div class="job-grid">""")
            current_score = score

        title = escape(j["title"] or "Untitled")
        url = escape(j["url"] or "")
        salary = escape(j["salary"] or "")
        location = escape(j["location"] or "")
        site = escape(j["site"] or "")
        site_color = colors.get(j["site"] or "", "#6b7280")
        apply_url = escape(j["application_url"] or "")

        reasoning_raw = j["score_reasoning"] or ""
        reasoning_lines = reasoning_raw.split("\n")
        keywords = reasoning_lines[0][:120] if reasoning_lines else ""
        reasoning = reasoning_lines[1][:200] if len(reasoning_lines) > 1 else ""

        # Slice before escape — only escape what we need
        desc_preview = escape((j["full_description"] or "")[:300])
        desc_len = len(j["full_description"] or "")

        meta_parts = [
            f'<span class="meta-tag site-tag" style="background:{site_color}33;color:{site_color}">{site}</span>'
        ]
        if salary:
            meta_parts.append(f'<span class="meta-tag salary">{salary}</span>')
        if location:
            meta_parts.append(f'<span class="meta-tag location">{location[:40]}</span>')
        meta_html = " ".join(meta_parts)

        apply_html = f'<a href="{apply_url}" class="apply-link" target="_blank">Apply</a>' if apply_url else ""
        lazy_desc = (
            f'<details class="full-desc-details" data-desc-url="{url}">'
            f'<summary class="expand-btn">Full Description ({desc_len:,} chars)</summary>'
            f'<div class="full-desc"></div></details>'
        ) if j["full_description"] else ""

        job_sections.append(f"""
        <div class="job-card" data-score="{score}" data-site="{escape(j['site'] or '')}" data-location="{location.lower()}">
          <div class="card-header">
            <span class="score-pill" style="background:{'#10b981' if score >= 7 else '#f59e0b'}">{score}</span>
            <a href="{url}" class="job-title" target="_blank">{title}</a>
          </div>
          <div class="meta-row">{meta_html}</div>
          {f'<div class="keywords-row">{escape(keywords)}</div>' if keywords else ''}
          {f'<div class="reasoning-row">{escape(reasoning)}</div>' if reasoning else ''}
          <p class="desc-preview">{desc_preview}...</p>
          {lazy_desc}
          <div class="card-footer">{apply_html}</div>
        </div>""")

    if current_score is not None:
        job_sections.append("</div>")
    job_sections_html = "".join(job_sections)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ApplyPilot Dashboard</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif; background: #0f172a; color: #e2e8f0; padding: 2rem; }}

  h1 {{ font-size: 1.8rem; font-weight: 700; margin-bottom: 0.5rem; }}
  .subtitle {{ color: #94a3b8; margin-bottom: 2rem; }}

  /* Summary cards */
  .summary {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 1rem; margin-bottom: 1.5rem; }}
  .stat-card {{ background: #1e293b; border-radius: 12px; padding: 1.25rem; }}
  .stat-num {{ font-size: 2rem; font-weight: 700; }}
  .stat-label {{ color: #94a3b8; font-size: 0.85rem; margin-top: 0.25rem; }}
  .stat-ok .stat-num {{ color: #10b981; }}
  .stat-scored .stat-num {{ color: #60a5fa; }}
  .stat-high .stat-num {{ color: #f59e0b; }}
  .stat-total .stat-num {{ color: #e2e8f0; }}
  .stat-pending .stat-num {{ color: #fb923c; }}
  .stat-approved .stat-num {{ color: #4ade80; }}
  .stat-rejected .stat-num {{ color: #f87171; }}

  /* Approval stats bar */
  .approval-bar {{ display: flex; gap: 1rem; margin-bottom: 2.5rem; flex-wrap: wrap; }}
  .approval-pill {{ background: #1e293b; border-radius: 8px; padding: 0.5rem 1rem; font-size: 0.85rem; }}
  .approval-pill .num {{ font-weight: 700; font-size: 1.1rem; margin-right: 0.3rem; }}
  .approval-pill.pending .num {{ color: #fb923c; }}
  .approval-pill.approved .num {{ color: #4ade80; }}
  .approval-pill.rejected .num {{ color: #f87171; }}
  .approval-pill.ready .num {{ color: #4ade80; }}

  /* Review queue */
  .review-section {{ margin-bottom: 2.5rem; }}
  .review-empty {{ background: #1e293b; border-radius: 12px; padding: 1.5rem; }}
  .review-heading {{ font-size: 1.3rem; font-weight: 700; margin-bottom: 1rem; display: flex; align-items: center; gap: 0.6rem; border-bottom: 3px solid #fb923c; padding-bottom: 0.5rem; }}
  .rq-badge {{ background: #fb923c; color: #0f172a; border-radius: 9999px; padding: 0.1rem 0.55rem; font-size: 0.8rem; font-weight: 700; }}
  .rq-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(420px, 1fr)); gap: 1rem; }}
  .rq-card {{ background: #1e293b; border-radius: 10px; padding: 1rem; border-left: 3px solid #fb923c; transition: all 0.2s; }}
  .rq-card.decided {{ opacity: 0.5; border-left-color: #334155; }}
  .rq-header {{ display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.5rem; flex-wrap: wrap; }}
  .btn-approve {{ background: #064e3b; color: #4ade80; border: 1px solid #16a34a; border-radius: 6px; padding: 0.35rem 0.85rem; font-size: 0.8rem; font-weight: 600; cursor: pointer; transition: all 0.15s; }}
  .btn-approve:hover {{ background: #065f46; }}
  .btn-approve:disabled {{ opacity: 0.5; cursor: not-allowed; }}
  .btn-reject {{ background: #450a0a; color: #f87171; border: 1px solid #dc2626; border-radius: 6px; padding: 0.35rem 0.85rem; font-size: 0.8rem; font-weight: 600; cursor: pointer; transition: all 0.15s; }}
  .btn-reject:hover {{ background: #7f1d1d; }}
  .btn-reject:disabled {{ opacity: 0.5; cursor: not-allowed; }}
  .reject-form {{ margin-top: 0.6rem; display: flex; gap: 0.5rem; flex-wrap: wrap; align-items: center; }}
  .note-input {{ background: #334155; border: 1px solid #475569; color: #e2e8f0; padding: 0.3rem 0.6rem; border-radius: 6px; font-size: 0.8rem; flex: 1; min-width: 160px; }}
  .btn-reject-confirm {{ background: #7f1d1d; color: #fca5a5; border: 1px solid #b91c1c; border-radius: 6px; padding: 0.3rem 0.7rem; font-size: 0.8rem; cursor: pointer; }}
  .btn-cancel {{ background: #334155; color: #94a3b8; border: none; border-radius: 6px; padding: 0.3rem 0.7rem; font-size: 0.8rem; cursor: pointer; }}
  .decided-badge {{ font-size: 0.8rem; font-weight: 600; padding: 0.25rem 0.6rem; border-radius: 6px; }}
  .decided-badge.approved {{ background: #064e3b; color: #4ade80; }}
  .decided-badge.rejected {{ background: #450a0a; color: #f87171; }}
  .apply-tag {{ background: #1e3a5f; color: #93c5fd; text-decoration: none; }}
  .apply-tag:hover {{ background: #1e40af; }}

  /* Ready to apply */
  .ready-section {{ margin-bottom: 2.5rem; }}
  .ready-heading {{ font-size: 1.3rem; font-weight: 700; margin-bottom: 1rem; display: flex; align-items: center; gap: 0.6rem; border-bottom: 3px solid #4ade80; padding-bottom: 0.5rem; }}
  .ready-badge {{ background: #4ade80; color: #0f172a; border-radius: 9999px; padding: 0.1rem 0.55rem; font-size: 0.8rem; font-weight: 700; }}
  .ready-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(420px, 1fr)); gap: 1rem; }}
  .ready-card {{ background: #1e293b; border-radius: 10px; padding: 1rem; border-left: 3px solid #4ade80; transition: all 0.2s; }}
  .ready-card.decided {{ opacity: 0.5; border-left-color: #334155; }}
  .btn-applied {{ background: #064e3b; color: #4ade80; border: 1px solid #16a34a; border-radius: 6px; padding: 0.35rem 0.85rem; font-size: 0.8rem; font-weight: 600; cursor: pointer; transition: all 0.15s; }}
  .btn-applied:hover {{ background: #065f46; }}
  .btn-applied:disabled {{ opacity: 0.5; cursor: not-allowed; }}
  .doc-row {{ display: flex; align-items: center; flex-wrap: wrap; gap: 0.4rem; margin-top: 0.4rem; font-size: 0.78rem; }}
  .doc-label {{ color: #64748b; width: 3.8rem; flex-shrink: 0; font-weight: 600; }}
  .doc-link {{ color: #4ade80; text-decoration: none; padding: 0.2rem 0.6rem; border: 1px solid #4ade8044; border-radius: 6px; }}
  .doc-link:hover {{ background: #4ade8022; }}
  .doc-link-alt {{ color: #94a3b8; text-decoration: none; padding: 0.2rem 0.5rem; border: 1px solid #47556944; border-radius: 6px; }}
  .doc-link-alt:hover {{ background: #33415566; color: #e2e8f0; }}
  .doc-missing {{ color: #64748b; font-style: italic; }}
  .stranded-note {{ font-size: 0.75rem; color: #fbbf24; background: #78350f33; border-radius: 6px; padding: 0.25rem 0.6rem; margin-bottom: 0.3rem; display: inline-block; }}
  .static-note {{ font-size: 0.8rem; color: #fbbf24; margin-bottom: 0.75rem; }}

  /* Filters */
  .filters {{ background: #1e293b; border-radius: 12px; padding: 1.25rem; margin-bottom: 2rem; display: flex; gap: 1rem; flex-wrap: wrap; align-items: center; }}
  .filter-label {{ color: #94a3b8; font-size: 0.85rem; font-weight: 600; }}
  .filter-btn {{ background: #334155; border: none; color: #94a3b8; padding: 0.4rem 0.8rem; border-radius: 6px; cursor: pointer; font-size: 0.8rem; transition: all 0.15s; }}
  .filter-btn:hover {{ background: #475569; color: #e2e8f0; }}
  .filter-btn.active {{ background: #60a5fa; color: #0f172a; font-weight: 600; }}
  .search-input {{ background: #334155; border: 1px solid #475569; color: #e2e8f0; padding: 0.4rem 0.8rem; border-radius: 6px; font-size: 0.8rem; width: 200px; }}
  .search-input::placeholder {{ color: #64748b; }}

  /* Score distribution */
  .score-section {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; margin-bottom: 2.5rem; }}
  .score-dist {{ background: #1e293b; border-radius: 12px; padding: 1.5rem; }}
  .score-dist h3 {{ font-size: 1rem; margin-bottom: 1rem; color: #94a3b8; }}
  .score-row {{ display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.4rem; }}
  .score-label {{ width: 1.5rem; text-align: right; font-size: 0.85rem; font-weight: 600; }}
  .score-bar-track {{ flex: 1; height: 14px; background: #334155; border-radius: 4px; overflow: hidden; }}
  .score-bar-fill {{ height: 100%; border-radius: 4px; transition: width 0.3s; }}
  .score-count {{ width: 2.5rem; font-size: 0.8rem; color: #94a3b8; }}

  /* Site bars */
  .sites-section {{ background: #1e293b; border-radius: 12px; padding: 1.5rem; }}
  .sites-section h3 {{ font-size: 1rem; margin-bottom: 1rem; color: #94a3b8; }}
  .site-row {{ margin-bottom: 0.8rem; }}
  .site-name {{ font-weight: 600; font-size: 0.9rem; }}
  .site-nums {{ color: #94a3b8; font-size: 0.75rem; margin: 0.15rem 0; }}
  .bar-track {{ height: 8px; background: #334155; border-radius: 4px; display: flex; overflow: hidden; }}
  .bar-fill {{ height: 100%; transition: width 0.3s; }}

  /* Score group headers */
  .score-header {{ font-size: 1.2rem; font-weight: 600; margin: 2.5rem 0 1rem; padding-bottom: 0.5rem; border-bottom: 3px solid; display: flex; align-items: center; gap: 0.75rem; }}
  .score-badge {{ display: inline-flex; align-items: center; justify-content: center; width: 2rem; height: 2rem; border-radius: 8px; color: #0f172a; font-weight: 700; font-size: 1rem; }}

  /* Job grid */
  .job-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(380px, 1fr)); gap: 1rem; }}
  .job-card {{ background: #1e293b; border-radius: 10px; padding: 1rem; border-left: 3px solid #334155; transition: all 0.15s; }}
  .job-card:hover {{ transform: translateY(-2px); box-shadow: 0 4px 12px #00000044; }}
  .job-card[data-score="9"], .job-card[data-score="10"] {{ border-left-color: #10b981; }}
  .job-card[data-score="8"] {{ border-left-color: #34d399; }}
  .job-card[data-score="7"] {{ border-left-color: #60a5fa; }}
  .job-card[data-score="6"] {{ border-left-color: #f59e0b; }}
  .job-card[data-score="5"] {{ border-left-color: #f59e0b88; }}

  .card-header {{ display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.5rem; }}
  .score-pill {{ display: inline-flex; align-items: center; justify-content: center; min-width: 1.6rem; height: 1.6rem; border-radius: 6px; color: #0f172a; font-weight: 700; font-size: 0.8rem; flex-shrink: 0; }}
  .job-title {{ color: #e2e8f0; text-decoration: none; font-weight: 600; font-size: 0.95rem; }}
  .job-title:hover {{ color: #60a5fa; }}

  .meta-row {{ display: flex; flex-wrap: wrap; gap: 0.4rem; margin-bottom: 0.4rem; }}
  .meta-tag {{ font-size: 0.72rem; padding: 0.15rem 0.5rem; border-radius: 4px; background: #334155; color: #94a3b8; }}
  .meta-tag.salary {{ background: #064e3b; color: #6ee7b7; }}
  .meta-tag.location {{ background: #1e3a5f; color: #93c5fd; }}

  .keywords-row {{ font-size: 0.75rem; color: #10b981; margin-bottom: 0.3rem; line-height: 1.4; }}
  .reasoning-row {{ font-size: 0.75rem; color: #94a3b8; margin-bottom: 0.5rem; font-style: italic; line-height: 1.4; }}

  .desc-preview {{ font-size: 0.8rem; color: #64748b; line-height: 1.5; margin-bottom: 0.75rem; max-height: 3.6em; overflow: hidden; }}
  .card-footer {{ display: flex; justify-content: flex-end; }}
  .apply-link {{ font-size: 0.8rem; color: #60a5fa; text-decoration: none; padding: 0.3rem 0.8rem; border: 1px solid #60a5fa33; border-radius: 6px; font-weight: 500; }}
  .apply-link:hover {{ background: #60a5fa22; }}

  .full-desc-details {{ margin-bottom: 0.75rem; }}
  .expand-btn {{ font-size: 0.8rem; color: #60a5fa; cursor: pointer; list-style: none; padding: 0.3rem 0; }}
  .expand-btn::-webkit-details-marker {{ display: none; }}
  .expand-btn:hover {{ color: #93c5fd; }}
  .full-desc {{ font-size: 0.8rem; color: #cbd5e1; line-height: 1.6; margin-top: 0.5rem; padding: 0.75rem; background: #0f172a; border-radius: 8px; max-height: 400px; overflow-y: auto; white-space: pre-wrap; word-break: break-word; }}

  .hidden {{ display: none !important; }}
  .job-count {{ color: #94a3b8; font-size: 0.85rem; margin-bottom: 1rem; }}
  code {{ background: #334155; padding: 0.1rem 0.4rem; border-radius: 4px; font-size: 0.85em; }}

  @media (max-width: 768px) {{
    .summary {{ grid-template-columns: repeat(2, 1fr); }}
    .score-section {{ grid-template-columns: 1fr; }}
    .job-grid, .rq-grid {{ grid-template-columns: 1fr; }}
    body {{ padding: 1rem; }}
  }}
</style>
</head>
<body>

<h1>ApplyPilot Dashboard</h1>
<p class="subtitle">{total} jobs &middot; {scored} scored &middot; {high_fit} strong matches (7+)</p>

<div class="summary">
  <div class="stat-card stat-total"><div class="stat-num">{total}</div><div class="stat-label">Total Jobs</div></div>
  <div class="stat-card stat-scored"><div class="stat-num">{scored}</div><div class="stat-label">Scored by LLM</div></div>
  <div class="stat-card stat-high"><div class="stat-num">{high_fit}</div><div class="stat-label">Strong Fit (7+)</div></div>
  <div class="stat-card stat-pending"><div class="stat-num" id="stat-pending">{pending_review}</div><div class="stat-label">Pending Review</div></div>
</div>

<div class="approval-bar">
  <div class="approval-pill pending"><span class="num" id="pill-pending">{pending_review}</span> pending review</div>
  <div class="approval-pill approved"><span class="num" id="pill-approved">{approved_count}</span> approved</div>
  <div class="approval-pill rejected"><span class="num" id="pill-rejected">{rejected_count}</span> rejected</div>
  <div class="approval-pill ready"><span class="num" id="pill-ready">{ready_count}</span> ready to apply</div>
</div>

{review_queue_html}

{ready_html}

<div class="filters">
  <span class="filter-label">Score:</span>
  <button class="filter-btn active" onclick="filterScore(0, event)">All 5+</button>
  <button class="filter-btn" onclick="filterScore(7, event)">7+ Strong</button>
  <button class="filter-btn" onclick="filterScore(8, event)">8+ Excellent</button>
  <button class="filter-btn" onclick="filterScore(9, event)">9+ Perfect</button>
  <span class="filter-label" style="margin-left:1rem">Search:</span>
  <input type="text" class="search-input" placeholder="Filter by title, site..." oninput="filterText(this.value)">
</div>

<div class="score-section">
  <div class="score-dist">
    <h3>Score Distribution</h3>
    {score_bars_html}
  </div>
  <div class="sites-section">
    <h3>By Source</h3>
    {site_rows_html}
  </div>
</div>

<div id="job-count" class="job-count"></div>

{job_sections_html}

<script>
let minScore = 0;
let searchText = '';
let pendingCount = {pending_review};
let approvedCount = {approved_count};
let rejectedCount = {rejected_count};
let readyCount = {ready_count};

function filterScore(min, event) {{
  minScore = min;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  event.target.classList.add('active');
  applyFilters();
}}

function filterText(text) {{
  searchText = text.toLowerCase();
  applyFilters();
}}

function applyFilters() {{
  let shown = 0;
  let total = 0;
  document.querySelectorAll('.job-card').forEach(card => {{
    total++;
    const score = parseInt(card.dataset.score) || 0;
    const text = card.textContent.toLowerCase();
    const scoreMatch = score >= (minScore || 5);
    const textMatch = !searchText || text.includes(searchText);
    if (scoreMatch && textMatch) {{
      card.classList.remove('hidden');
      shown++;
    }} else {{
      card.classList.add('hidden');
    }}
  }});
  document.getElementById('job-count').textContent = `Showing ${{shown}} of ${{total}} jobs`;
  document.querySelectorAll('.score-header').forEach(header => {{
    const grid = header.nextElementSibling;
    if (grid && grid.classList.contains('job-grid')) {{
      const visible = grid.querySelectorAll('.job-card:not(.hidden)').length;
      header.style.display = visible ? '' : 'none';
      grid.style.display = visible ? '' : 'none';
    }}
  }});
}}

function updateCounters() {{
  document.getElementById('stat-pending').textContent = pendingCount;
  document.getElementById('pill-pending').textContent = pendingCount;
  document.getElementById('pill-approved').textContent = approvedCount;
  document.getElementById('pill-rejected').textContent = rejectedCount;
  const badge = document.getElementById('rq-badge');
  if (badge) badge.textContent = pendingCount;
  const pillReady = document.getElementById('pill-ready');
  if (pillReady) pillReady.textContent = readyCount;
  const readyBadge = document.getElementById('ready-badge');
  if (readyBadge) readyBadge.textContent = readyCount;
}}

function submitReviewCard(btn, action) {{
  const card = btn.closest('.rq-card');
  submitReview(card, action);
}}

function showRejectFormCard(btn) {{
  btn.closest('.rq-card').querySelector('.reject-form').style.display = 'flex';
}}

function hideRejectFormCard(btn) {{
  btn.closest('.reject-form').style.display = 'none';
}}

function submitRejectWithNoteCard(btn) {{
  const card = btn.closest('.rq-card');
  const note = card.querySelector('.note-input').value;
  submitReview(card, 'rejected', note);
}}

async function submitReview(card, action, note) {{
  const url = card.dataset.url;
  const btns = card.querySelectorAll('button');
  btns.forEach(b => b.disabled = true);

  try {{
    const resp = await fetch('/api/review', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ url, action, note: note || '' }})
    }});
    if (!resp.ok) throw new Error(await resp.text());

    card.classList.add('decided');
    const header = card.querySelector('.rq-header');
    card.querySelectorAll('.btn-approve, .btn-reject').forEach(b => b.remove());
    const badge = document.createElement('span');
    badge.className = `decided-badge ${{action}}`;
    badge.textContent = action === 'approved' ? '✓ Approved' : '✗ Rejected';
    if (header) header.appendChild(badge);
    card.querySelector('.reject-form').style.display = 'none';

    if (action === 'approved') {{ pendingCount--; approvedCount++; }}
    else {{ pendingCount--; rejectedCount++; }}
    updateCounters();
  }} catch (err) {{
    btns.forEach(b => b.disabled = false);
    alert('Error: ' + err.message + '\\n\\nMake sure the dashboard server is running:\\n  applypilot dashboard');
  }}
}}

async function markApplied(btn) {{
  const card = btn.closest('.ready-card');
  const url = card.dataset.url;
  btn.disabled = true;

  try {{
    const resp = await fetch('/api/applied', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ url }})
    }});
    if (!resp.ok) throw new Error(await resp.text());

    // Keep the card visible but dimmed so a misclick is obvious;
    // it disappears on the next reload.
    card.classList.add('decided');
    btn.remove();
    const header = card.querySelector('.rq-header');
    const badge = document.createElement('span');
    badge.className = 'decided-badge approved';
    badge.textContent = '✓ Applied';
    if (header) header.appendChild(badge);

    readyCount--;
    updateCounters();
  }} catch (err) {{
    btn.disabled = false;
    alert('Error: ' + err.message + '\\n\\nMake sure the dashboard server is running:\\n  applypilot dashboard');
  }}
}}

applyFilters();

// Lazy-load full descriptions when <details> is opened
document.querySelectorAll('.full-desc-details[data-desc-url]').forEach(details => {{
  details.addEventListener('toggle', async function() {{
    if (!this.open) return;
    const div = this.querySelector('.full-desc');
    if (div.dataset.loaded) return;
    div.dataset.loaded = '1';
    div.style.color = '#64748b';
    div.textContent = 'Loading…';
    try {{
      const resp = await fetch('/api/description?url=' + encodeURIComponent(this.dataset.descUrl));
      div.textContent = await resp.text();
      div.style.color = '';
      div.style.whiteSpace = 'pre-wrap';
    }} catch(e) {{
      div.textContent = 'Failed to load (server not running).';
    }}
  }});
}});
</script>

</body>
</html>"""


def generate_dashboard(output_path: str | None = None) -> str:
    """Generate a static HTML dashboard snapshot.

    Args:
        output_path: Where to write the HTML file. Defaults to ~/.applypilot/dashboard.html.

    Returns:
        Absolute path to the generated HTML file.
    """
    out = Path(output_path) if output_path else APP_DIR / "dashboard.html"
    conn = get_connection()
    # Static snapshot: no /api/* routes, so interactive controls render inert
    html = _build_html(conn, live=False)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")

    abs_path = str(out.resolve())
    console.print(f"[green]Dashboard written to {abs_path}[/green]")
    return abs_path


def open_dashboard(output_path: str | None = None) -> None:
    """Generate the dashboard and open it in the default browser.

    Args:
        output_path: Where to write the HTML file. Defaults to ~/.applypilot/dashboard.html.
    """
    path = generate_dashboard(output_path)
    console.print("[dim]Opening in browser...[/dim]")
    webbrowser.open(f"file:///{path}")


def serve_dashboard(port: int = 7410) -> None:
    """Start a local HTTP server serving the interactive dashboard.

    Handles:
      GET  /            — live dashboard HTML (reads DB on each request)
      POST /api/review  — approve or reject a job: {url, action, note?}
      POST /api/applied — mark a job as applied by hand: {url}
      GET  /api/pending — current approval counts as JSON
      GET  /api/file    — serve a generated resume/cover letter:
                          ?url=<job>&kind=resume|cover&fmt=pdf|txt
    """
    from applypilot.config import COVER_LETTER_DIR, TAILORED_DIR
    from applypilot.database import get_approval_stats, mark_applied_manual, set_approval

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # silence default access log
            pass

        def _send_json(self, data: dict, status: int = 200) -> None:
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            from urllib.parse import parse_qs
            parsed = urlparse(self.path)

            if parsed.path == "/api/pending":
                stats = get_approval_stats()
                self._send_json(stats)
                return

            if parsed.path == "/api/description":
                params = parse_qs(parsed.query)
                job_url = params.get("url", [""])[0]
                row = get_connection().execute(
                    "SELECT full_description FROM jobs WHERE url = ?", (job_url,)
                ).fetchone()
                text = (row[0] or "") if row else ""
                body = text.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if parsed.path == "/api/file":
                params = parse_qs(parsed.query)
                job_url = params.get("url", [""])[0]
                kind = params.get("kind", ["resume"])[0]
                fmt = params.get("fmt", ["pdf"])[0]

                # Column name is chosen from a fixed pair, never interpolated
                # from the query string; the filesystem path then comes from
                # the DB row, so there is no traversal vector.
                if kind == "resume":
                    column, root = "tailored_resume_path", TAILORED_DIR
                elif kind == "cover":
                    column, root = "cover_letter_path", COVER_LETTER_DIR
                else:
                    self.send_response(400)
                    self.end_headers()
                    return

                row = get_connection().execute(
                    f"SELECT {column} FROM jobs WHERE url = ?", (job_url,)
                ).fetchone()
                if not row or not row[0]:
                    self.send_response(404)
                    self.end_headers()
                    return

                path = Path(row[0])
                if fmt == "pdf":
                    path = path.with_suffix(".pdf")

                # Defence in depth: the file must live in the directory the
                # generator writes to, even if the DB row were tampered with.
                try:
                    resolved = path.resolve()
                    resolved.relative_to(Path(root).resolve())
                except (ValueError, OSError):
                    self.send_response(403)
                    self.end_headers()
                    return

                if not resolved.is_file():
                    self.send_response(404)
                    self.end_headers()
                    return

                body = resolved.read_bytes()
                ctype = "application/pdf" if fmt == "pdf" else "text/plain; charset=utf-8"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Disposition", f'inline; filename="{resolved.name}"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if parsed.path == "/":
                conn = get_connection()
                html = _build_html(conn)
                body = html.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            self.send_response(404)
            self.end_headers()

        def do_POST(self):
            if self.path == "/api/review":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                try:
                    data = json.loads(body)
                    url = data.get("url", "").strip()
                    action = data.get("action", "").strip()
                    note = data.get("note", "").strip()
                    if not url or action not in ("approved", "rejected"):
                        self._send_json({"error": "url and action (approved|rejected) required"}, 400)
                        return
                    set_approval(url, action, notes=note)
                    self._send_json({"ok": True})
                except Exception as exc:
                    # Never let a bad request kill the server thread
                    log.exception("Review request failed")
                    self._send_json({"error": str(exc)}, 500)
                return

            if self.path == "/api/applied":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                try:
                    data = json.loads(body)
                    url = data.get("url", "").strip()
                    if not url:
                        self._send_json({"error": "url required"}, 400)
                        return
                    mark_applied_manual(url)
                    self._send_json({"ok": True})
                except Exception as exc:
                    log.exception("Mark-applied request failed")
                    self._send_json({"error": str(exc)}, 500)
                return

            self.send_response(404)
            self.end_headers()

    server = HTTPServer(("127.0.0.1", port), _Handler)
    url = f"http://127.0.0.1:{port}"

    def _open_browser():
        import time
        time.sleep(0.4)
        webbrowser.open(url)

    threading.Thread(target=_open_browser, daemon=True).start()

    console.print(f"\n[bold green]ApplyPilot Dashboard[/bold green] running at [bold]{url}[/bold]")
    console.print("  Approve/Reject jobs in the [bold]Review Queue[/bold] section.")
    console.print("  After reviewing, run [bold]applypilot run tailor cover pdf[/bold] to process approved jobs.")
    console.print("  Then use [bold]Ready to Apply[/bold] to open each posting with its resume and cover letter.")
    console.print("  Press [bold]Ctrl+C[/bold] to stop.\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        console.print("\n[dim]Dashboard server stopped.[/dim]")
