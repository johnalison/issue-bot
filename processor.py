"""
Per-issue job pipeline: clone deps → clone repo → claude → commit → MR.

Job directory layout (with parent_dep: barista):
  worktrees/{owner}-{repo}-{iid}/
    barista/              ← dependency clone
      coffea4bees/        ← main repo cloned inside parent dep
"""

import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

import state
from config import Config, RepoConfig
from gitlab_client import GitLabClient


def process_issue(issue: dict, repo_cfg: RepoConfig, cfg: Config, gl: GitLabClient):
    project = repo_cfg.full_name
    num = issue["iid"]
    title = issue["title"]
    body = issue.get("description") or ""
    issue_url = issue["web_url"]

    job_dir = cfg.worktree_base / f"{repo_cfg.owner}-{repo_cfg.name}-{num}"
    log_file = cfg.log_dir / f"{repo_cfg.owner}-{repo_cfg.name}-{num}.log"
    session_file = cfg.log_dir / f"{repo_cfg.owner}-{repo_cfg.name}-{num}.jsonl"

    job_log = logging.getLogger(f"job.{project}.{num}")
    job_log.setLevel(logging.DEBUG)
    fh = logging.FileHandler(log_file)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    job_log.addHandler(fh)

    if not state.claim_job(project, num, str(job_dir)):
        job_log.info("Job already claimed — skipping")
        return

    job_log.info(f"Starting job for {project}!{num}: {title!r}")
    status = "failed"
    mr_url = None
    session_id = None

    try:
        job_dir.mkdir(parents=True, exist_ok=True)

        # 1. Clone dependencies; track their paths for PYTHONPATH
        dep_dirs: dict[str, Path] = {}
        for dep in repo_cfg.dependencies:
            dep_dir = job_dir / dep.name
            job_log.info(f"Cloning dependency {dep.full_name} into {dep_dir.name}/")
            clone_url = gl.get_clone_url(dep.full_name)
            _run(["git", "clone", "--depth", "1", "-b", dep.base_branch,
                   clone_url, str(dep_dir)], job_log)
            dep_dirs[dep.name] = dep_dir
            job_log.info(f"  {dep.name} cloned OK")

        # 2. Determine where to clone the main repo
        if repo_cfg.parent_dep:
            if repo_cfg.parent_dep not in dep_dirs:
                raise RuntimeError(
                    f"parent_dep '{repo_cfg.parent_dep}' not found in dependencies"
                )
            repo_dir = dep_dirs[repo_cfg.parent_dep] / repo_cfg.name
            job_log.info(f"Cloning {project} into {repo_cfg.parent_dep}/{repo_cfg.name}/")
        else:
            repo_dir = job_dir / "repo"
            job_log.info(f"Cloning {project} into repo/")

        clone_url = gl.get_clone_url(project)
        _run(["git", "clone", "--depth", "1", "-b", repo_cfg.base_branch,
               clone_url, str(repo_dir)], job_log)

        # 3. Build PYTHONPATH from dependency directories
        pythonpath = ":".join(str(p) for p in dep_dirs.values())
        if existing := os.environ.get("PYTHONPATH"):
            pythonpath = f"{pythonpath}:{existing}"
        job_log.info(f"PYTHONPATH includes: {list(dep_dirs.keys())}")

        # 4. Invoke Claude from the parent dep dir (if any) for broader context,
        #    otherwise from the repo dir itself.
        claude_cwd = dep_dirs[repo_cfg.parent_dep] if repo_cfg.parent_dep else repo_dir
        prompt = _build_prompt(title, body, issue_url, repo_cfg, repo_dir, claude_cwd)
        job_log.info(f"Invoking Claude Code from {claude_cwd.name}/")

        claude_result = subprocess.run(
            [cfg.claude_bin, "-p", prompt,
             "--output-format", "stream-json",
             "--verbose",
             "--dangerously-skip-permissions"],
            cwd=str(claude_cwd),
            capture_output=True,
            text=True,
            timeout=cfg.claude_timeout_seconds,
            env=_claude_env(cfg, pythonpath),
        )

        # 5. Save session transcript
        session_file.write_text(claude_result.stdout)
        session_id = _extract_session_id(claude_result.stdout)
        if session_id:
            job_log.info(f"Claude session ID: {session_id}  (resume: claude --resume {session_id})")
        else:
            job_log.warning("Could not extract Claude session ID")

        if claude_result.returncode != 0:
            job_log.error(f"Claude exited {claude_result.returncode}: {claude_result.stderr[:500]}")
            status = "failed"
            return

        # 6. Check for changes in the main repo only
        porcelain = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo_dir), capture_output=True, text=True
        )
        if not porcelain.stdout.strip():
            job_log.info("Claude made no changes — skipped")
            status = "skipped"
            return

        # 7. Commit and push on a new branch
        branch = _branch_name(num, title)
        job_log.info(f"Creating branch {branch}")
        _run(["git", "config", "user.email", "issue-bot@falcon"], job_log, cwd=repo_dir)
        _run(["git", "config", "user.name", "Issue Bot"], job_log, cwd=repo_dir)
        _run(["git", "checkout", "-b", branch], job_log, cwd=repo_dir)
        _run(["git", "add", "-A"], job_log, cwd=repo_dir)
        _run(["git", "commit", "-m",
               f"fix: address issue #{num} — {title[:60]}\n\nAutomated fix via Claude Code.\nSee: {issue_url}"],
              job_log, cwd=repo_dir)
        _run(["git", "push", "origin", branch], job_log, cwd=repo_dir)

        # 8. Open MR
        mr_url = gl.create_mr(
            project_path=project,
            title=f"[bot] Fix #{num}: {title[:60]}",
            description=_mr_body(issue, session_id),
            source_branch=branch,
            target_branch=repo_cfg.base_branch,
        )
        job_log.info(f"MR opened: {mr_url}")
        job_log.info("Worktree kept until issue is closed on GitLab")
        status = "mr_open"

    except subprocess.TimeoutExpired:
        job_log.error(f"Claude timed out after {cfg.claude_timeout_seconds}s")
        status = "timeout"
    except Exception as e:
        job_log.exception(f"Unexpected error: {e}")
        status = "failed"
    finally:
        keep_worktree = status == "mr_open"
        state.release_job(project, num, status, pr_url=mr_url, session_id=session_id,
                          worktree_path=str(job_dir) if keep_worktree else None)
        job_log.info(f"Job finished: status={status}")
        if not keep_worktree and job_dir.exists():
            shutil.rmtree(job_dir, ignore_errors=True)
        job_log.removeHandler(fh)
        fh.close()


def _build_prompt(title: str, body: str, url: str, repo_cfg: RepoConfig,
                  repo_dir: Path, claude_cwd: Path) -> str:
    dep_note = ""
    if repo_cfg.dependencies:
        dep_names = [d.name for d in repo_cfg.dependencies]
        dep_note = (
            f"\nDependency packages ({', '.join(dep_names)}) are available on PYTHONPATH "
            f"and can be imported directly.\n"
        )

    edit_scope = ""
    if repo_cfg.parent_dep:
        rel = repo_dir.relative_to(claude_cwd)
        edit_scope = (
            f"\nYou are running from the `{claude_cwd.name}/` directory. "
            f"The issue is filed against `{repo_cfg.name}`, which is located at `{rel}/`. "
            f"Make all code edits inside `{rel}/` unless the issue explicitly involves `{claude_cwd.name}` itself.\n"
        )

    return f"""You are being invoked headlessly by the automated issue-bot on falcon.phys.cmu.edu.

You are working inside a git repository. A GitLab issue has been filed that needs fixing.

Issue title: {title}
Issue URL: {url}

Issue description:
{body}
{dep_note}{edit_scope}
Instructions:
- Investigate the codebase and implement a fix for the issue described above.
- Make all necessary edits to source files.
- Do NOT run git commands, open MRs, or push branches — that is handled externally.
- Do NOT add tests unless explicitly requested in the issue.
- If the issue is ambiguous, make changes using your best judgement rather than doing nothing.
- When done, briefly summarize what you changed and why, and list any judgement calls you made.
"""


def _extract_session_id(stream_json: str) -> str | None:
    for line in stream_json.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if sid := obj.get("session_id"):
                return sid
        except json.JSONDecodeError:
            continue
    return None


def _branch_name(issue_iid: int, title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower())[:40].strip("-")
    return f"issue-bot/{issue_iid}-{slug}"


def _mr_body(issue: dict, session_id: str | None) -> str:
    resume_note = (
        f"\n\n**Resume Claude session:** `claude --resume {session_id}` (on falcon)"
        if session_id else ""
    )
    return (
        f"Closes #{issue['iid']}\n\n"
        f"This MR was generated automatically by the issue bot using Claude Code.\n"
        f"**Please review all changes carefully before merging.**"
        f"{resume_note}"
    )


def _claude_env(cfg: Config, pythonpath: str) -> dict:
    env = os.environ.copy()
    # Auth via claude login subscription (no API key needed)
    # Must unset CLAUDECODE to allow headless invocation from within a Claude session
    env.pop("CLAUDECODE", None)
    if pythonpath:
        env["PYTHONPATH"] = pythonpath
    return env


def _run(cmd: list, log: logging.Logger, cwd: Path = None) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    if result.returncode != 0:
        raise RuntimeError(f"`{' '.join(cmd)}` failed:\n{result.stderr}")
    return result.stdout
