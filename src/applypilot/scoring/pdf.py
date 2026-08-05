"""Text-to-PDF conversion for tailored resumes and cover letters.

Parses the structured text resume format, renders via an HTML/CSS template,
and exports to PDF using headless Chromium via Playwright.
"""

import logging
import re
from html import escape
from pathlib import Path

from applypilot.config import COVER_LETTER_DIR, TAILORED_DIR

log = logging.getLogger(__name__)

# Shared page setup, so a cover letter looks like it belongs with the resume.
_PAGE_CSS = """
@page {
    size: letter;
    margin: 0.35in 0.5in;
}
* {
    margin: 0;
    padding: 0;
    box-sizing: border-box;
}
body {
    font-family: 'Calibri', 'Segoe UI', Arial, sans-serif;
    font-size: 10pt;
    line-height: 1.35;
    color: #1a1a1a;
}
"""


# ── Resume Parser ────────────────────────────────────────────────────────

def parse_resume(text: str) -> dict:
    """Parse a structured text resume into sections.

    Expects a format with header lines (name, title, location, contact)
    followed by ALL-CAPS section headers (SUMMARY, TECHNICAL SKILLS, etc.).

    Args:
        text: Full resume text.

    Returns:
        {"name": str, "title": str, "location": str, "contact": str, "sections": dict}
    """
    lines = [line.rstrip() for line in text.strip().split("\n")]

    # Header: first few lines before SUMMARY
    header_lines: list[str] = []
    body_start = 0
    for i, line in enumerate(lines):
        if line.strip().upper() == "SUMMARY":
            body_start = i
            break
        if line.strip():
            header_lines.append(line.strip())

    name = header_lines[0] if len(header_lines) > 0 else ""
    title = header_lines[1] if len(header_lines) > 1 else ""
    # The header may have 3 or 4 lines depending on whether location is included
    location = ""
    contact = ""
    if len(header_lines) > 3:
        location = header_lines[2]
        contact = header_lines[3]
    elif len(header_lines) > 2:
        # Could be location or contact -- check for email/phone indicators
        if "@" in header_lines[2] or "|" in header_lines[2]:
            contact = header_lines[2]
        else:
            location = header_lines[2]

    # Split body into sections by ALL-CAPS headers
    sections: dict[str, str] = {}
    current_section: str | None = None
    current_lines: list[str] = []

    for line in lines[body_start:]:
        stripped = line.strip()
        # Detect section headers (all caps, no leading dash/bullet, longer than 3 chars)
        if (
            stripped
            and stripped == stripped.upper()
            and not stripped.startswith("-")
            and len(stripped) > 3
            and not stripped.startswith("\u2022")
        ):
            if current_section:
                sections[current_section] = "\n".join(current_lines).strip()
            current_section = stripped
            current_lines = []
        else:
            current_lines.append(line)

    if current_section:
        sections[current_section] = "\n".join(current_lines).strip()

    return {
        "name": name,
        "title": title,
        "location": location,
        "contact": contact,
        "sections": sections,
    }


def parse_skills(text: str) -> list[tuple[str, str]]:
    """Parse skills section into (category, value) pairs.

    Args:
        text: The TECHNICAL SKILLS section text.

    Returns:
        List of (category_name, skills_string) tuples.
    """
    skills: list[tuple[str, str]] = []
    for raw_line in text.strip().split("\n"):
        line = raw_line.strip()
        if ":" in line:
            cat, val = line.split(":", 1)
            skills.append((cat.strip(), val.strip()))
    return skills


def parse_entries(text: str) -> list[dict]:
    """Parse experience/project entries from section text.

    Args:
        text: The EXPERIENCE or PROJECTS section text.

    Returns:
        List of {"title": str, "subtitle": str, "bullets": list[str]} dicts.
    """
    entries: list[dict] = []
    lines = text.strip().split("\n")
    current: dict | None = None

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("- ", "\u2022 ")):
            if current:
                current["bullets"].append(stripped[2:].strip())
        elif current is None or (
            not stripped.startswith("-")
            and not stripped.startswith("\u2022")
            and len(current.get("bullets", [])) > 0
        ):
            # New entry
            if current:
                entries.append(current)
            current = {"title": stripped, "subtitle": "", "bullets": []}
        elif current and not current["subtitle"]:
            current["subtitle"] = stripped
        else:
            if current:
                current["bullets"].append(stripped)

    if current:
        entries.append(current)

    return entries


# ── HTML Template ────────────────────────────────────────────────────────

def build_html(resume: dict) -> str:
    """Build professional resume HTML from parsed data.

    Args:
        resume: Parsed resume dict from parse_resume().

    Returns:
        Complete HTML string ready for PDF rendering.
    """
    sections = resume["sections"]

    # Every parsed value is escaped at interpolation -- the surrounding markup
    # is ours, but the text came from an LLM and may contain & or <.

    # Skills
    skills_html = ""
    if "TECHNICAL SKILLS" in sections:
        skills = parse_skills(sections["TECHNICAL SKILLS"])
        rows = ""
        for cat, val in skills:
            rows += f'<div class="skill-row"><span class="skill-cat">{escape(cat)}:</span> {escape(val)}</div>\n'
        skills_html = f'<div class="section"><div class="section-title">Technical Skills</div>{rows}</div>'

    # Experience
    exp_html = ""
    if "EXPERIENCE" in sections:
        entries = parse_entries(sections["EXPERIENCE"])
        items = ""
        for e in entries:
            bullets = "".join(f"<li>{escape(b)}</li>" for b in e["bullets"])
            subtitle = f'<div class="entry-subtitle">{escape(e["subtitle"])}</div>' if e["subtitle"] else ""
            items += (f'<div class="entry"><div class="entry-title">{escape(e["title"])}</div>'
                      f'{subtitle}<ul>{bullets}</ul></div>')
        exp_html = f'<div class="section"><div class="section-title">Experience</div>{items}</div>'

    # Projects
    proj_html = ""
    if "PROJECTS" in sections:
        entries = parse_entries(sections["PROJECTS"])
        items = ""
        for e in entries:
            bullets = "".join(f"<li>{escape(b)}</li>" for b in e["bullets"])
            subtitle = f'<div class="entry-subtitle">{escape(e["subtitle"])}</div>' if e["subtitle"] else ""
            items += (f'<div class="entry"><div class="entry-title">{escape(e["title"])}</div>'
                      f'{subtitle}<ul>{bullets}</ul></div>')
        proj_html = f'<div class="section"><div class="section-title">Projects</div>{items}</div>'

    # Education
    edu_html = ""
    if "EDUCATION" in sections:
        edu_text = escape(sections["EDUCATION"].strip())
        edu_html = f'<div class="section"><div class="section-title">Education</div><div class="edu">{edu_text}</div></div>'

    # Summary
    summary_html = ""
    if "SUMMARY" in sections:
        summary_text = escape(sections["SUMMARY"].strip())
        summary_html = (f'<div class="section"><div class="section-title">Summary</div>'
                        f'<div class="summary">{summary_text}</div></div>')

    # Contact line parsing
    contact = resume["contact"]
    contact_parts = [escape(p.strip()) for p in contact.split("|")] if contact else []
    contact_html = " &nbsp;|&nbsp; ".join(contact_parts)

    # Location line (may be empty)
    location_html = f'<div class="location">{escape(resume["location"])}</div>' if resume["location"] else ""

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
{_PAGE_CSS}
.header {{
    text-align: center;
    margin-bottom: 4px;
    padding-bottom: 4px;
    border-bottom: 1.5px solid #2a7ab5;
}}
.name {{
    font-size: 18pt;
    font-weight: 700;
    color: #1a3a5c;
    letter-spacing: 0.5px;
}}
.title {{
    font-size: 10.5pt;
    color: #3a6b8c;
    margin: 1px 0;
}}
.location {{
    font-size: 9pt;
    color: #555;
}}
.contact {{
    font-size: 9pt;
    color: #444;
    margin-top: 1px;
}}
.contact a {{
    color: #2c3e50;
    text-decoration: none;
}}
.section {{
    margin-top: 5px;
}}
.section-title {{
    font-size: 10pt;
    font-weight: 700;
    color: #1a3a5c;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    border-bottom: 1.5px solid #2a7ab5;
    padding-bottom: 1px;
    margin-bottom: 3px;
}}
.summary {{
    font-size: 9.5pt;
    color: #333;
    line-height: 1.4;
}}
.skill-row {{
    font-size: 9.5pt;
    margin: 0;
    line-height: 1.35;
}}
.skill-cat {{
    font-weight: 600;
    color: #1a3a5c;
}}
.entry {{
    margin-bottom: 4px;
    break-inside: avoid;
}}
.entry-title {{
    font-weight: 600;
    font-size: 10pt;
    color: #1a3a5c;
}}
.entry-subtitle {{
    font-size: 9pt;
    color: #4a7a9b;
    font-style: italic;
    margin-bottom: 1px;
}}
ul {{
    margin-left: 14px;
    padding: 0;
}}
li {{
    font-size: 9.5pt;
    margin-bottom: 1px;
    line-height: 1.35;
}}
.edu {{
    font-size: 10pt;
}}
</style>
</head>
<body>
<div class="header">
    <div class="name">{escape(resume['name'])}</div>
    <div class="title">{escape(resume['title'])}</div>
    {location_html}
    <div class="contact">{contact_html}</div>
</div>
{summary_html}
{skills_html}
{exp_html}
{proj_html}
{edu_html}
</body>
</html>"""


# ── Cover Letter Parser + Template ───────────────────────────────────────

def _is_salutation(block: str) -> bool:
    """True if a block looks like 'Dear Hiring Manager,' rather than prose."""
    if "\n" in block or len(block) > 80:
        return False
    lowered = block.lower()
    return lowered.startswith(("dear ", "to ", "hello", "hi ")) or block.endswith(",")


def _is_closing(block: str) -> bool:
    """True if a block looks like a sign-off ('Sincerely,\\nTolu') or a bare name.

    Sentence-ending punctuation rules a block out: "Happy to discuss further."
    is a closing paragraph, not a signature.
    """
    lines = block.split("\n")
    if len(lines) > 3 or len(block) > 120:
        return False
    return all(len(ln) <= 60 and not ln.rstrip().endswith((".", "!", "?", ":")) for ln in lines)


def parse_cover_letter(text: str) -> dict:
    """Split a cover letter into salutation, body paragraphs, and sign-off.

    Cover letters have none of the structure parse_resume() expects -- no
    SUMMARY line, no ALL-CAPS section headers -- so they need their own parser.
    Feeding one to parse_resume() sweeps every paragraph into the contact
    header and silently drops the signature.

    Every non-empty block of the source is preserved in exactly one of the
    returned fields; nothing is discarded.

    Args:
        text: Full cover letter text.

    Returns:
        {"salutation": str, "paragraphs": list[str], "closing": list[str]}
    """
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text.strip()) if b.strip()]

    salutation = ""
    if blocks and _is_salutation(blocks[0]):
        salutation = blocks.pop(0)

    # len(blocks) > 1 guard: a single-block letter is all body, never a signature
    closing: list[str] = []
    if len(blocks) > 1 and _is_closing(blocks[-1]):
        closing = [ln.strip() for ln in blocks.pop().split("\n") if ln.strip()]

    return {"salutation": salutation, "paragraphs": blocks, "closing": closing}


def build_cover_letter_html(letter: dict) -> str:
    """Build business-letter HTML from parse_cover_letter() output.

    Args:
        letter: Parsed dict from parse_cover_letter().

    Returns:
        Complete HTML string ready for PDF rendering.
    """
    salutation_html = (
        f'<div class="cl-salutation">{escape(letter["salutation"])}</div>'
        if letter["salutation"] else ""
    )
    # Interior newlines are soft-wrapped by the LLM, not meaningful breaks
    paragraphs_html = "".join(
        f'<p class="cl-para">{escape(" ".join(p.split()))}</p>'
        for p in letter["paragraphs"]
    )
    closing_html = ""
    if letter["closing"]:
        lines = "".join(f'<div>{escape(ln)}</div>' for ln in letter["closing"])
        closing_html = f'<div class="cl-closing">{lines}</div>'

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
{_PAGE_CSS}
body {{
    font-size: 10.5pt;
    line-height: 1.5;
}}
.cl-salutation {{
    font-size: 11pt;
    color: #1a3a5c;
    font-weight: 600;
    margin-bottom: 10px;
}}
.cl-para {{
    margin-bottom: 10px;
    text-align: justify;
}}
.cl-closing {{
    margin-top: 16px;
    font-size: 11pt;
    color: #1a3a5c;
}}
</style>
</head>
<body>
{salutation_html}
{paragraphs_html}
{closing_html}
</body>
</html>"""


_RESUME_SECTIONS = frozenset({"SUMMARY", "TECHNICAL SKILLS", "EXPERIENCE", "PROJECTS", "EDUCATION"})


def detect_kind(text_path: Path | None, text: str) -> str:
    """Classify a document as "resume" or "cover".

    Resume evidence wins: rendering a cover letter with the resume template is
    ugly, but rendering a resume with the letter template would drop every
    section. So an ALL-CAPS resume header always forces "resume", whatever the
    filename says.
    """
    if any(ln.strip().upper() in _RESUME_SECTIONS for ln in text.splitlines()):
        return "resume"
    if text_path is not None and Path(text_path).stem.endswith("_CL"):
        return "cover"
    first = next((ln.strip() for ln in text.strip().splitlines() if ln.strip()), "")
    if _is_salutation(first):
        return "cover"
    return "resume"


# ── PDF Renderer ─────────────────────────────────────────────────────────

def render_pdf(html: str, output_path: str) -> None:
    """Render HTML to PDF using Playwright's headless Chromium.

    Args:
        html: Complete HTML string.
        output_path: Path to write the PDF file.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(html, wait_until="networkidle")
        page.pdf(
            path=output_path,
            format="Letter",
            margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
            print_background=True,
        )
        browser.close()


# ── Public API ───────────────────────────────────────────────────────────

def convert_to_pdf(
    text_path: Path, output_path: Path | None = None, html_only: bool = False,
    kind: str | None = None,
) -> Path:
    """Convert a text resume or cover letter to PDF.

    Args:
        text_path: Path to the .txt file to convert.
        output_path: Optional override for the output path. Defaults to same
            name with .pdf extension.
        html_only: If True, output HTML instead of PDF.
        kind: "resume" or "cover". When None, inferred from the _CL.txt
            filename suffix, defaulting to "resume".

    Returns:
        Path to the generated PDF (or HTML) file.
    """
    text_path = Path(text_path)
    text = text_path.read_text(encoding="utf-8")

    if kind is None:
        kind = detect_kind(text_path, text)

    html = (build_cover_letter_html(parse_cover_letter(text)) if kind == "cover"
            else build_html(parse_resume(text)))

    if html_only:
        out = output_path or text_path.with_suffix(".html")
        out = Path(out)
        out.write_text(html, encoding="utf-8")
        log.info("HTML generated: %s", out)
        return out

    out = output_path or text_path.with_suffix(".pdf")
    out = Path(out)
    render_pdf(html, str(out))
    log.info("PDF generated: %s", out)
    return out


def scan_pending(limit: int = 0) -> list[tuple[Path, str]]:
    """Find generated .txt files that don't have a PDF yet.

    Whether a PDF is pending is a filesystem question -- there is no pdf path
    column in the database -- so both the converter and the pipeline's pending
    count go through here.

    Args:
        limit: Maximum number of files to return; 0 means no limit.

    Returns:
        List of (path, kind) pairs, where kind is "resume" or "cover".
    """
    pending: list[tuple[Path, str]] = []
    for directory, kind in ((TAILORED_DIR, "resume"), (COVER_LETTER_DIR, "cover")):
        if not directory.exists():
            log.warning("Directory does not exist: %s", directory)
            continue
        for f in sorted(directory.glob("*.txt")):
            # _JOB.txt is the archived job description, not a document to render
            if f.name.endswith("_JOB.txt"):
                continue
            if f.with_suffix(".pdf").exists():
                continue
            pending.append((f, kind))
            if limit and len(pending) >= limit:
                return pending
    return pending


def batch_convert(limit: int = 50) -> int:
    """Convert tailored resumes and cover letters that don't have PDFs yet.

    Scans TAILORED_DIR (excluding the _JOB.txt job-description copies) and
    COVER_LETTER_DIR, checks whether a .pdf with the same stem already exists,
    and converts any that are missing. Each directory is rendered with its own
    template -- a cover letter run through the resume template loses its body
    and signature.

    Args:
        limit: Maximum number of files to convert across both directories.

    Returns:
        Number of PDFs generated.
    """
    to_convert = scan_pending(limit=limit)

    if not to_convert:
        log.info("All text files already have PDFs.")
        return 0

    log.info("Converting %d files to PDF...", len(to_convert))
    converted = 0
    for f, kind in to_convert:
        try:
            convert_to_pdf(f, kind=kind)
            converted += 1
        except Exception:
            log.exception("Failed to convert %s", f.name)

    log.info("Done: %d/%d PDFs generated", converted, len(to_convert))
    return converted
