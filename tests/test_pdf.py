"""Tests for text-to-PDF conversion.

None of these launch Chromium: parsing and HTML building are pure, and the one
batch test that needs an output file uses the fake_render fixture. CI does not
run `playwright install`, so a real render would fail there.

All sample data is fabricated -- never the real profile from ~/.applypilot.
"""

from applypilot.scoring.pdf import (
    batch_convert,
    build_cover_letter_html,
    build_html,
    detect_kind,
    parse_cover_letter,
    parse_resume,
    scan_pending,
)

# Carries the four cases that matter: a bare-name signature, a literal "&",
# a literal "<", and a paragraph that must NOT be mistaken for a signature.
SAMPLE_COVER_LETTER = """\
Dear Hiring Manager,

I built a Q&A service over 500 policy documents with <1s p99 latency.

At Example Corp I cut deployment steps from 18 to under 5.

Happy to walk through any of this in more detail.

Jane
"""

SAMPLE_RESUME = """\
Jane Q. Example
Senior Widget Engineer
Remote, Canada
jane@example.com | 555-0100

SUMMARY
Builds reliable systems at scale.

TECHNICAL SKILLS
DevOps & Infra: Kubernetes, Docker

EXPERIENCE
Widget Corp - Senior Engineer
- Cut p99 latency to <1s across 500 services.

EDUCATION
BSc, Example University
"""


# ── Cover letter parsing ─────────────────────────────────────────────────

def test_parse_cover_letter_extracts_salutation():
    assert parse_cover_letter(SAMPLE_COVER_LETTER)["salutation"] == "Dear Hiring Manager,"


def test_parse_cover_letter_preserves_bare_name_signature():
    """Direct regression: parse_resume() silently dropped the signature."""
    assert parse_cover_letter(SAMPLE_COVER_LETTER)["closing"] == ["Jane"]


def test_parse_cover_letter_keeps_trailing_sentence_as_body():
    """A short closing sentence must not be mistaken for a signature block."""
    paragraphs = parse_cover_letter(SAMPLE_COVER_LETTER)["paragraphs"]
    assert any("Happy to walk through" in p for p in paragraphs)


def test_parse_cover_letter_loses_no_content():
    """The strongest guarantee: every non-empty source line is retained."""
    parsed = parse_cover_letter(SAMPLE_COVER_LETTER)
    retained = " ".join(
        [parsed["salutation"], *parsed["paragraphs"], *parsed["closing"]]
    )
    for line in SAMPLE_COVER_LETTER.strip().splitlines():
        if line.strip():
            assert line.strip() in retained


def test_parse_cover_letter_without_salutation_keeps_body():
    parsed = parse_cover_letter("Straight into the pitch, no greeting here.")
    assert parsed["salutation"] == ""
    assert parsed["paragraphs"] == ["Straight into the pitch, no greeting here."]


# ── Cover letter rendering ───────────────────────────────────────────────

def test_cover_letter_html_contains_all_content():
    html = build_cover_letter_html(parse_cover_letter(SAMPLE_COVER_LETTER))
    assert "Dear Hiring Manager," in html
    assert "Example Corp" in html
    assert "Jane" in html


def test_cover_letter_html_escapes_ampersand_and_angle_brackets():
    html = build_cover_letter_html(parse_cover_letter(SAMPLE_COVER_LETTER))
    assert "Q&amp;A" in html
    assert "&lt;1s" in html
    assert "Q&A service" not in html


def test_cover_letter_html_has_no_resume_chrome():
    html = build_cover_letter_html(parse_cover_letter(SAMPLE_COVER_LETTER))
    assert "section-title" not in html
    assert 'class="name"' not in html


def test_cover_letter_shares_resume_font_stack():
    """The two documents should read as a set."""
    letter = build_cover_letter_html(parse_cover_letter(SAMPLE_COVER_LETTER))
    resume = build_html(parse_resume(SAMPLE_RESUME))
    font = "'Calibri', 'Segoe UI', Arial, sans-serif"
    assert font in letter
    assert font in resume


# ── Resume regression ────────────────────────────────────────────────────

def test_resume_still_renders_all_sections():
    html = build_html(parse_resume(SAMPLE_RESUME))
    for heading in ("Summary", "Technical Skills", "Experience", "Education"):
        assert f">{heading}<" in html


def test_resume_html_escapes_ampersand_and_angle_brackets():
    html = build_html(parse_resume(SAMPLE_RESUME))
    assert "DevOps &amp; Infra" in html
    assert "&lt;1s" in html


def test_resume_contact_separator_not_double_escaped():
    """Highest-risk line: escaping after the join would corrupt every resume."""
    html = build_html(parse_resume(SAMPLE_RESUME))
    assert "&nbsp;|&nbsp;" in html
    assert "&amp;nbsp;" not in html


def test_resume_generated_markup_not_escaped():
    """Only parser output gets escaped; assembled markup must pass through."""
    html = build_html(parse_resume(SAMPLE_RESUME))
    assert 'class="section-title"' in html
    assert "&lt;div" not in html


# ── Kind detection ───────────────────────────────────────────────────────

def test_detect_kind_by_cl_suffix(tmp_path):
    p = tmp_path / "indeed_Engineer_CL.txt"
    assert detect_kind(p, SAMPLE_COVER_LETTER) == "cover"


def test_detect_kind_resume_sections_win_over_filename(tmp_path):
    """Safety guard: a resume must never reach the letter template."""
    p = tmp_path / "misnamed_CL.txt"
    assert detect_kind(p, SAMPLE_RESUME) == "resume"


def test_detect_kind_sniffs_salutation_without_suffix(tmp_path):
    assert detect_kind(tmp_path / "letter.txt", SAMPLE_COVER_LETTER) == "cover"


# ── Batch conversion ─────────────────────────────────────────────────────

def test_scan_pending_covers_both_directories(doc_dirs):
    tailored, covers = doc_dirs
    (tailored / "a.txt").write_text(SAMPLE_RESUME, encoding="utf-8")
    (covers / "b_CL.txt").write_text(SAMPLE_COVER_LETTER, encoding="utf-8")

    found = {p.name: kind for p, kind in scan_pending()}
    assert found == {"a.txt": "resume", "b_CL.txt": "cover"}


def test_scan_pending_skips_job_description_dumps(doc_dirs):
    tailored, _ = doc_dirs
    (tailored / "a_JOB.txt").write_text("raw job posting", encoding="utf-8")
    assert scan_pending() == []


def test_scan_pending_skips_files_that_already_have_a_pdf(doc_dirs):
    tailored, _ = doc_dirs
    (tailored / "a.txt").write_text(SAMPLE_RESUME, encoding="utf-8")
    (tailored / "a.pdf").write_bytes(b"%PDF-1.4")
    assert scan_pending() == []


def test_batch_convert_renders_each_kind_with_its_own_template(doc_dirs, fake_render):
    """End-to-end proof the two-directory scan doesn't mangle cover letters."""
    tailored, covers = doc_dirs
    (tailored / "a.txt").write_text(SAMPLE_RESUME, encoding="utf-8")
    (covers / "b_CL.txt").write_text(SAMPLE_COVER_LETTER, encoding="utf-8")

    assert batch_convert() == 2

    rendered = {str(path): html for html, path in fake_render}
    resume_html = next(h for p, h in rendered.items() if p.endswith("a.pdf"))
    letter_html = next(h for p, h in rendered.items() if p.endswith("b_CL.pdf"))

    assert 'class="section-title"' in resume_html
    assert "cl-salutation" in letter_html
    assert "Jane" in letter_html  # signature survived


def test_batch_convert_clears_the_pending_queue(doc_dirs, fake_render):
    """Pins the pipeline fix: the count must actually reach zero."""
    tailored, covers = doc_dirs
    (tailored / "a.txt").write_text(SAMPLE_RESUME, encoding="utf-8")
    (covers / "b_CL.txt").write_text(SAMPLE_COVER_LETTER, encoding="utf-8")

    pending = len(scan_pending())
    assert batch_convert() == pending
    assert scan_pending() == []


def test_batch_convert_returns_zero_when_nothing_pending(doc_dirs):
    assert batch_convert() == 0
