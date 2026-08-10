"""Shared filename helper for tailored resumes and cover letters."""

import hashlib
import re


def safe_doc_prefix(site: str, title: str, url: str) -> str:
    """Build a filesystem-safe, collision-resistant prefix for a job's documents.

    Two different jobs can easily share an identical (site, title) pair --
    "Senior AI/ML Engineer" on LinkedIn recurs across many companies -- and a
    bare {site}_{title} name would silently let one job's resume overwrite
    another's. Appending a short hash of the job URL (the DB primary key)
    disambiguates them while keeping the filename human-readable.
    """
    safe_title = re.sub(r"[^\w\s-]", "", title)[:50].strip().replace(" ", "_")
    safe_site = re.sub(r"[^\w\s-]", "", site)[:20].strip().replace(" ", "_")
    url_hash = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
    return f"{safe_site}_{safe_title}_{url_hash}"
