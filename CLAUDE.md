# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Setup

```bash
# Install editable (python-jobspy must be installed separately due to dependency conflicts)
pip install -e ".[dev]"
pip install --no-deps python-jobspy && pip install pydantic tls-client requests markdownify regex

# One-time user config
applypilot init
applypilot doctor   # verify all tiers
```

## Common Commands

```bash
# Lint (line length 120, target py311)
ruff check src/
ruff format src/

# Run pipeline stages (combinable)
applypilot run                          # all stages
applypilot run discover enrich          # specific stages only
applypilot run --workers 4 --stream     # parallel + streaming

# Auto-apply (Tier 3 only)
applypilot apply --dry-run              # preview without submitting
applypilot apply --workers 2            # parallel Chrome workers

# Other
applypilot review     # human approval gate before apply
applypilot status     # pipeline statistics
applypilot dashboard  # interactive web UI
```

## Architecture

### 6-Stage Pipeline (`src/applypilot/pipeline.py`)

```
discover → enrich → score → tailor → cover → pdf → (apply)
```

Each stage reads pending jobs from SQLite, processes them, and writes results back. `--stream` mode runs stages concurrently; default is sequential.

### Tier System

| Tier | Requirements | Capabilities |
|------|-------------|--------------|
| 1 | Python 3.11+ | Discovery only |
| 2 | + LLM API key | + Scoring, tailoring, cover letters, PDF |
| 3 | + Claude Code CLI + Chrome | + Auto-apply via browser automation |

### Key Modules

| Module | Responsibility |
|--------|---------------|
| `cli.py` | Typer CLI app — all user-facing commands |
| `pipeline.py` | Stage orchestration, streaming mode, worker pool |
| `database.py` | SQLite (WAL mode), thread-local connections, all DB schema |
| `llm.py` | LLM provider abstraction (Gemini → OpenAI → local Ollama) |
| `config.py` | Path resolution, defaults, env loading from `~/.applypilot/.env` |
| `discovery/jobspy.py` | JobSpy scraping (Indeed, LinkedIn, Glassdoor, ZipRecruiter) |
| `discovery/workday.py` | Workday employer portal API scraping |
| `discovery/smartextract.py` | Direct career site scraping with LLM selector generation |
| `enrichment/detail.py` | 3-tier cascade: JSON-LD → CSS selectors → LLM extraction |
| `scoring/scorer.py` | LLM fit scoring (1–10) |
| `scoring/tailor.py` | LLM resume tailoring with validation, up to 5 retry attempts |
| `scoring/cover_letter.py` | LLM cover letter generation |
| `scoring/validator.py` | Banned-word and fabrication detection |
| `apply/launcher.py` | Chrome worker management, Claude Code subprocess orchestration |
| `apply/chrome.py` | Chrome CDP connection, Playwright MCP server setup |
| `view.py` | Rich-based dashboard and review UI |

### User Data Directory (`~/.applypilot/` or `$APPLYPILOT_DIR`)

```
profile.json      # name, email, skills, work auth, EEO defaults — injected at runtime
resume.txt        # plain-text resume required for LLM stages
searches.yaml     # search queries, locations, site selection, score threshold
.env              # GEMINI_API_KEY / OPENAI_API_KEY / LLM_URL / CAPSOLVER_API_KEY
applypilot.db     # SQLite job database (WAL mode)
resumes/          # tailored resume outputs per job
cover_letters/    # generated cover letter outputs
```

### LLM Provider (`llm.py`)
Auto-detected in priority order: Gemini (default, free tier 15 RPM) → OpenAI → local (Ollama/llama.cpp via `LLM_URL`). Retries on 429/503 with exponential backoff (10s base, 60s cap).

### Validation Strictness (`scoring/validator.py`)
Three modes controlled by `--validation` flag:
- `strict` — banned words are errors, LLM judge required (most API calls)
- `normal` — banned words are warnings (recommended for Gemini free tier)
- `lenient` — no banned-word checks or judge (fastest)

### Auto-Apply Stack (`apply/`)
Spawns isolated Chrome instances per worker (separate user-data dirs, CDP ports 9222+N). Each worker runs a Playwright MCP server via `npx` and passes a structured prompt to a Claude Code CLI subprocess for form navigation and submission. Requires Node.js 18+ and Chrome/Chromium on PATH or `CHROME_PATH`.

### Parallelism
`ThreadPoolExecutor` for discovery and enrichment stages. Thread-local SQLite connections prevent contention. Chrome workers isolated by port and user-data directory.

## Shipped Config (`src/applypilot/config/`)

- `sites.yaml` — job board URLs, blocked SSO domains, manual ATS domains (skip auto-apply), base URL registry for URL resolution
- `employers.yaml` — 48 preconfigured Workday employer portals
- `searches.example.yaml` — template shown during `applypilot init`
