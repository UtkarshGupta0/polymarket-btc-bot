# GitHub Repo Publication — Design Spec

**Date:** 2026-04-24
**Author:** operator (UtkarshGupta0) + Claude
**Status:** design approved, awaiting implementation plan

## Problem

The `polymarket-btc-bot` project lives as a local directory with working code, tests, and internal docs, but is not yet suitable for GitHub publication. It has no root `README.md`, no `LICENSE`, no `.gitignore`, no `requirements.txt`. Sensitive files (`.env`, `.env.save*`) and large runtime artifacts (`logs/`, `__pycache__/`) are tracked or would be tracked on next `git add`. Publication without these guardrails risks leaking wallet private keys and committing megabytes of paper-trade logs on every push.

## Goal

Prepare the repo for first publication as a **public open-source** GitHub project so the operator can push it under their account (`UtkarshGupta0/polymarket-btc-bot`). Anyone who clones should be able to read the README and get the bot running in paper mode on Linux, macOS, or Windows within ~10 minutes. Live trading is covered as a flagged advanced section.

## Non-goals

- CI/CD setup (GitHub Actions, build badges) — deferred.
- `pyproject.toml` or packaged distribution on PyPI — `requirements.txt` is sufficient.
- CHANGELOG, issue/PR templates, contributing guide — overkill for a personal public repo.
- Renaming or consolidating existing `docs/HANDOVER/` tree — leave as-is; README links into it.
- Moving `POLYMARKET_BOT_GAMEPLAN.md` or deleting `.env.save*` files — no scope creep beyond what's needed to publish safely.
- Modifying any bot code, tests, or existing docs.

## Scope

All changes are additive at the repo root (`/home/utk/polymarket-btc-bot/`) or are `.gitignore`-mediated. No edits to any existing `.py`, `docs/HANDOVER/`, `docs/superpowers/specs/`, `tests/`, or `scripts/` file.

**Approach chosen:** Minimal publish — add only what's missing, leave everything else alone. Cleanup (moving gameplan doc, renaming HANDOVER/, deleting stale `.env.save*`) was explicitly rejected as out-of-scope.

## File inventory

### Create (new)

| Path | Purpose |
|---|---|
| `README.md` | Single-file landing page with TOC, install per-OS, config, run, architecture, features, troubleshooting, license |
| `LICENSE` | MIT, copyright 2026 UtkarshGupta0 |
| `.gitignore` | Secrets, Python artifacts, venvs, logs, IDE/OS noise, internal planning |
| `requirements.txt` | Loose-ranged pins: `aiohttp`, `websockets`, `requests` |

### Modify

| Path | Change |
|---|---|
| `.env.example` | Audit against `config.py` `os.environ.get(...)` calls; add any missing env vars as commented placeholders. If already complete, no change. |

### Remove from git tracking (one-time)

After committing the new `.gitignore`, run:

```
git rm -r --cached __pycache__ .pytest_cache logs docs/superpowers/plans
```

These paths remain on disk but are untracked thereafter. `.env` and `.env.save*` are already in a permission-restricted state and (per current `git status`) not tracked — `.gitignore` makes them stay that way.

### Leave untouched

All `.py` files, `docs/HANDOVER/`, `docs/superpowers/specs/`, `tests/`, `scripts/`, `POLYMARKET_BOT_GAMEPLAN.md`, `dashboard.py`.

## README.md structure

Single file, expected ~400–600 lines. Section order:

1. **Title + badges** — Python 3.10+, MIT license, paper-mode status
2. **One-paragraph what-it-does** — binary 5-min BTC markets, maker-only, edge-based entry, Kelly sizing
3. **Table of Contents** — anchor links to sections below
4. **Overview** — ~100 words, strategy in plain English
5. **How it works** — 4–5 sentences on signal → gate → reprice → post → resolution; link to `docs/HANDOVER/02_strategy.md`
6. **Requirements** — Python 3.10+, pip, git; optional Polymarket account + USDC for live
7. **Install — Linux/WSL** — `git clone`, `python -m venv venv`, `source venv/bin/activate`, `pip install -r requirements.txt`, `cp .env.example .env`
8. **Install — macOS** — same + note on Homebrew Python (`brew install python@3.11`) and Xcode Command Line Tools if aiohttp compile fails
9. **Install — Windows** — PowerShell, `python -m venv venv`, `venv\Scripts\Activate.ps1`, same `pip install`; note on `Set-ExecutionPolicy -Scope Process Bypass` if script execution blocked
10. **Configuration (`.env`)** — markdown table: var name, default, description, example; link to `docs/HANDOVER/03_configuration.md` for full detail
11. **Running (paper mode)** — `python bot.py`, log file location in `logs/`, stop with Ctrl-C
12. **Running (live mode)** — ⚠️ warning callout; requires funded wallet + real private key in `.env`; `PAPER_MODE=false`; link to `docs/HANDOVER/07_going_live.md`
13. **Architecture** — short module table: `bot.py` (orchestration), `price_feed.py` (BTC ticks), `signal_engine.py` (features + signal), `executor.py` (maker order post), `risk_manager.py` (sizing, balance), `market_finder.py` (Polymarket window lookup), `trade_logger.py` (JSON records), `telegram_alerts.py` (notifications), `backtester.py` (historical sim), `self_improver.py` (advisory)
14. **Features** — bullet list: maker-only zero-fee, fractional Kelly sizing after warmup, configurable gates (MIN_CONFIDENCE, MIN_EDGE, TRADING_HOURS_BLOCK, MIN_DELTA_PCT), integrated backtester, self-improver advisory, dashboard, Telegram alerts, counterfactual gate validator
15. **Testing** — `python tests/test_*.py` (plain-assert convention, no pytest runner required); each file is individually executable
16. **Troubleshooting** — websockets install on Windows, aiohttp compile on macOS M1 (need Xcode CLI), port conflict on dashboard, no BTC ticks (check Binance WS URL)
17. **Further reading** — link list to all `docs/HANDOVER/*.md` and `docs/superpowers/specs/*.md`
18. **License + disclaimer** — MIT; educational only, no financial advice, crypto markets can lose money, use at your own risk

## .gitignore contents

```gitignore
# Secrets — NEVER commit
.env
.env.save
.env.save.*
*.key
*.pem

# Python
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
.pytest_cache/
.mypy_cache/
.ruff_cache/
*.egg-info/
build/
dist/

# Virtual environments
venv/
.venv/
env/
ENV/

# Runtime data
logs/
*.log

# Internal planning
docs/superpowers/plans/

# IDE / OS
.vscode/
.idea/
*.swp
*.swo
.DS_Store
Thumbs.db
desktop.ini
```

Rationale: secrets group first (most critical — leaked bot private key drains the wallet). Python group covers compile artifacts. Logs group reflects operator's scope decision to keep paper PnL data private. `docs/superpowers/plans/` excluded because it is scratch implementation work; specs (in `docs/superpowers/specs/`) are kept because they document design intent.

## LICENSE contents

Standard MIT text, copyright line:

```
Copyright (c) 2026 UtkarshGupta0
```

Rest of the license is the 2-paragraph MIT boilerplate (permission grant + no-warranty disclaimer). Full text is well-known; no customization needed.

## requirements.txt contents

```
aiohttp>=3.9,<4.0
websockets>=12.0,<14.0
requests>=2.31,<3.0
```

Derived from `grep '^import\|^from' *.py`:
- `aiohttp` — `price_feed.py`, `market_finder.py`
- `websockets` — `price_feed.py`
- `requests` — `backtester.py`

No other third-party imports. `asyncio`, `json`, `logging`, `datetime`, `pathlib`, `dataclasses`, `typing`, `collections`, `statistics` are stdlib. Loose ranges (not `==` pins) let pip pick wheels compatible with the user's Python + OS combo, which matters because Windows and macOS M1 may not have binary wheels for every version.

## .env.example audit

The existing `.env.example` is in a permission-restricted directory I cannot read. The implementation task must:

1. Read `.env.example` (operator runs the read, or grants temporary access).
2. Grep `config.py` for every `os.environ.get("X", ...)` / `os.environ["X"]`.
3. Confirm every var surfaced in `config.py` has a commented entry in `.env.example`.
4. If any are missing, add them as commented placeholders with sensible defaults and a one-line purpose comment.

If `.env.example` is already complete, no edit is made.

## Verification workflow (before first push)

Run in order:

1. **Purge cached artifacts:** `git rm -r --cached __pycache__ .pytest_cache logs docs/superpowers/plans`
2. **Confirm clean status:** `git status` — no `.env`, no `logs/`, no `.pyc`, no `plans/` staged
3. **Secret scan:** `git grep -i 'private_key\|0x[a-f0-9]\{40\}' -- '*.py' '*.md' '*.txt' '*.example'` — any hit must be a placeholder or comment, never a real value
4. **Fresh-install test:** `python -m venv /tmp/test_venv && source /tmp/test_venv/bin/activate && pip install -r requirements.txt && python -c "import bot; import signal_engine; import executor; import price_feed"` — imports succeed
5. **Test suite smoke:** `for f in tests/test_*.py; do python "$f"; done` — no failures
6. **Commit + push:** `git add README.md LICENSE .gitignore requirements.txt .env.example` (and any untracked files), commit, push to `git@github.com:UtkarshGupta0/polymarket-btc-bot.git`

If any step fails, halt and fix before pushing.

## Output

After implementation + verification, repo has:
- Root: `README.md`, `LICENSE`, `.gitignore`, `requirements.txt`, `.env.example` (verified), plus unchanged code/docs/tests/scripts.
- No tracked secrets, no tracked logs, no tracked Python cache, no tracked internal plans.
- A clean first push that is clone-and-runnable on Linux/macOS/Windows.

## Success criteria

After push, the operator can:
- Share the GitHub URL publicly without fear of wallet exposure.
- A stranger can clone → `python -m venv venv` → `pip install -r requirements.txt` → `cp .env.example .env` → fill in paper-mode values → `python bot.py` and see paper trades within a few 5-minute windows.
- The README answers "what does this do?" and "how do I run it?" without the reader opening any other file.
- Further-reading links in the README lead to the existing `docs/HANDOVER/` content for readers who want depth.

## Rollback

`git rm README.md LICENSE .gitignore requirements.txt && git commit` — reverts to pre-publication state. `.gitignore` removal re-exposes cached paths but they'll already be untracked at the filesystem-commit level, so nothing re-appears as tracked automatically.

## Risks

1. **Missing env var in `.env.example`.** Mitigation: explicit audit step in implementation plan.
2. **Third-party wheel missing on Windows/M1.** Mitigation: loose version ranges in `requirements.txt`, troubleshooting section in README documents the known aiohttp/websockets edge cases.
3. **Large files in git history from before this change.** Mitigation: out of scope — if log files were committed in prior history, `git filter-repo` is the tool, but operator has not indicated this is a problem; assume fresh public repo inherits only current tree cleanly. If history pollution is discovered, address in a follow-up.
4. **`.env` content accidentally leaked in `docs/HANDOVER/` or specs.** Mitigation: secret-scan step grep in verification workflow catches real private keys or addresses.

## Out of scope (explicit)

- GitHub Actions CI
- `pyproject.toml` / PyPI packaging
- CHANGELOG, CONTRIBUTING, issue templates
- Refactoring or renaming existing directories
- Deleting or moving `POLYMARKET_BOT_GAMEPLAN.md`
- Deleting `.env.save*` backups
- Git history rewriting
- Dependabot / security scanning config

## Files created

- `README.md` (new)
- `LICENSE` (new)
- `.gitignore` (new)
- `requirements.txt` (new)
- `.env.example` (possibly modified, possibly untouched)
- `docs/superpowers/specs/2026-04-24-github-repo-design.md` (this file)
