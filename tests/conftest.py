"""Shared test fixtures and isolation.

config.APP_DIR is resolved at import time from $APPLYPILOT_DIR, so the override
below must run before anything imports applypilot.config. pytest imports the
rootdir conftest before collecting test modules, which guarantees that ordering.
"""

import os
import tempfile
from pathlib import Path

# Set unconditionally, not with setdefault: an inherited real value pointing at
# the developer's ~/.applypilot would defeat the purpose.
os.environ["APPLYPILOT_DIR"] = tempfile.mkdtemp(prefix="applypilot-tests-")

import pytest  # noqa: E402  (must follow the env override above)


@pytest.fixture
def doc_dirs(tmp_path, monkeypatch):
    """Point pdf.py's output directories at a temp dir.

    pdf.py binds TAILORED_DIR/COVER_LETTER_DIR into its own namespace at import,
    so patching applypilot.config is not enough -- patch the pdf module globals.
    """
    from applypilot.scoring import pdf

    tailored = tmp_path / "tailored_resumes"
    covers = tmp_path / "cover_letters"
    tailored.mkdir()
    covers.mkdir()
    monkeypatch.setattr(pdf, "TAILORED_DIR", tailored)
    monkeypatch.setattr(pdf, "COVER_LETTER_DIR", covers)
    return tailored, covers


@pytest.fixture
def fake_render(monkeypatch):
    """Replace render_pdf so no test ever launches Chromium.

    CI does not run `playwright install`, so a real render would fail there.
    Records (html, output_path) and writes a stub file so downstream .exists()
    checks behave like the real thing.
    """
    calls = []

    def _stub(html_str, output_path):
        calls.append((html_str, output_path))
        Path(output_path).write_bytes(b"%PDF-1.4 stub")

    from applypilot.scoring import pdf

    monkeypatch.setattr(pdf, "render_pdf", _stub)
    return calls
