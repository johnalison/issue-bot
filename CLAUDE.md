# CLAUDE.md — issue-bot

Automated GitLab issue bot on `falcon.phys.cmu.edu`. Monitors CERN GitLab repos for
issues with trigger keywords and uses Claude Code (headless) to attempt fixes, then
opens a merge request.

## System context

- **Server:** falcon.phys.cmu.edu (Ubuntu 24.04, Python 3.12)
- **User:** jalison
- **Claude Code binary:** `~/.local/bin/claude` (v2.1.62)
- **Python venv:** `~/issue-bot/venv` — use `venv/bin/python` for all testing
- **Working dir:** `~/issue-bot/`

## Architecture

```
bot.py              # Polling loop + ThreadPoolExecutor; entry point
config.py           # Loads config.yaml + env vars into Config/RepoConfig dataclasses
gitlab_client.py    # CERN GitLab REST API (issues, MR creation, clone URLs)
processor.py        # Per-job pipeline: clone deps → clone repo → claude → commit → MR
state.py            # SQLite state: processed issues, active jobs, poll timestamps
config.yaml         # User-editable: repos, keywords, deps, poll interval
logs/               # bot.log + per-job {owner}-{repo}-{iid}.log and .jsonl (Claude transcript)
worktrees/          # Ephemeral per-job directories (cleaned up after MR)
venv/               # Python virtualenv (requests, PyYAML)
```

## Target repo and dependency layout

- **Main repo:** `https://gitlab.cern.ch/cms-cmu/coffea4bees`
- **Dependency:** `https://gitlab.cern.ch/cms-cmu/barista`
- **coffea4bees must be cloned inside barista** (`parent_dep: barista` in config.yaml)

Per-job directory layout:
```
worktrees/cms-cmu-coffea4bees-{iid}/
  barista/              ← shallow clone of barista
    coffea4bees/        ← shallow clone of coffea4bees (Claude works here)
```

- `PYTHONPATH` is set to include `barista/` so Claude can import it when running code
- Git operations (branch, commit, push) happen only inside `barista/coffea4bees/`
- The worktree is kept after the MR is opened (status: `mr_open`); it is deleted once the issue is closed on GitLab

## Credentials

```bash
export GITLAB_TOKEN=glpat-...   # CERN GitLab PAT with 'api' scope — set in ~/.bashrc
# NO ANTHROPIC_API_KEY needed — Claude runs via subscription auth (~/.claude/)
```

`GITLAB_TOKEN` is used for both API calls and HTTPS git clone:
`https://oauth2:{token}@gitlab.cern.ch/...`

**Claude auth:** Uses Claude.ai subscription (Max plan), not API key. Credentials stored in
`~/.claude/` by `claude auth login`. The bot subprocess inherits the environment and uses
those credentials automatically. Do NOT set `ANTHROPIC_API_KEY`.

## Current status (as of 2026-03-09)

All modules written and tested for GitLab connectivity.

### Done
- [x] `state.py` — SQLite + WAL, atomic claim_job, stale job recovery
- [x] `config.py` — YAML + env loading, `parent_dep` support for nested clone layout
- [x] `gitlab_client.py` — issues, MR creation, clone URL, rate-limit handling
- [x] `processor.py` — full pipeline, barista cloned first, coffea4bees inside it, PYTHONPATH set
- [x] `bot.py` — polling loop, thread pool, signal handling, crash recovery
- [x] `GITLAB_TOKEN` confirmed in `~/.bashrc`, GitLab API connection verified
- [x] Confirmed `ANTHROPIC_API_KEY` not needed; subscription auth used instead

### TODO — pick up here
- [ ] End-to-end test: file a test issue on coffea4bees with keyword `bot:fix`, watch bot clone, run Claude, open MR
- [ ] Verify barista is importable from within a coffea4bees worktree (PYTHONPATH check)
- [ ] Set up as persistent process (systemd user service or tmux session)

## Key design decisions

- **SQLite + WAL** — atomic `claim_job` INSERT OR IGNORE prevents duplicate jobs across poll windows
- **`--depth 1` clone** per job, not `git worktree` — avoids object-store locking, trivially deletable
- **`parent_dep: barista`** in config — coffea4bees cloned inside barista dir; PYTHONPATH set to barista root
- **`ThreadPoolExecutor(max_workers=2)`** — subprocess-bound workload
- **`--output-format stream-json`** — full Claude transcript saved to `.jsonl`; session ID in state DB + MR body
- **`created_after` polling** — tracks last poll timestamp per repo, avoids full issue list each cycle
- **SSH clone** — uses SSH key already configured on falcon for git operations

## Running

```bash
cd ~/issue-bot
# GITLAB_TOKEN already exported via ~/.bashrc
venv/bin/python bot.py
```

## Claude session resumption

Each job saves:
- `logs/{owner}-{repo}-{iid}.jsonl` — full stream-json transcript
- Session ID in `state.db` and in the MR description

To resume: `~/.local/bin/claude --resume <session_id>`

## Debugging

```bash
# Recent processed issues
venv/bin/python -c "
import sqlite3; c = sqlite3.connect('state.db'); c.row_factory = sqlite3.Row
for r in c.execute('SELECT * FROM processed_issues ORDER BY processed_at DESC LIMIT 10'):
    print(dict(r))
"

# Active jobs (should be empty when bot not running)
venv/bin/python -c "
import sqlite3; c = sqlite3.connect('state.db'); c.row_factory = sqlite3.Row
for r in c.execute('SELECT * FROM active_jobs'): print(dict(r))
"

# Force re-process an issue
venv/bin/python -c "
import sqlite3; c = sqlite3.connect('state.db')
c.execute(\"DELETE FROM processed_issues WHERE repo='cms-cmu/coffea4bees' AND issue_number=N\")
c.commit()
"
```
