"""
Main entry point: polling loop + thread pool.
"""

import logging
import shutil
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import state
import config as cfg_module
from gitlab_client import GitLabClient
from processor import process_issue


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/bot.log"),
    ]
)
log = logging.getLogger("bot")


def keyword_match(issue: dict, keywords: list[str]) -> bool:
    text = f"{issue.get('title','')} {issue.get('description') or ''}".lower()
    return any(kw.lower() in text for kw in keywords)


def startup_checks(cfg):
    if not shutil.which(cfg.claude_bin):
        log.error(f"claude not found at {cfg.claude_bin}")
        sys.exit(1)

    stale = state.stale_active_jobs(max_age_seconds=900)
    for job in stale:
        log.warning(f"Recovering stale job {job['repo']}#{job['issue_number']}")
        state.release_job(job["repo"], job["issue_number"], "failed")
        from pathlib import Path
        if wp := job.get("worktree_path"):
            shutil.rmtree(wp, ignore_errors=True)


def cleanup_resolved_issues(gl: GitLabClient):
    """Delete worktrees for issues that have been closed on GitLab."""
    for job in state.get_open_mr_jobs():
        repo = job["repo"]
        num = job["issue_number"]
        try:
            issue = gl.get_issue(repo, num)
            if issue.get("state") == "closed":
                log.info(f"Issue {repo}#{num} closed — cleaning up worktree")
                if wp := job.get("worktree_path"):
                    shutil.rmtree(wp, ignore_errors=True)
                state.mark_completed(repo, num)
        except Exception as e:
            log.warning(f"Could not check issue {repo}#{num}: {e}")


def main():
    cfg = cfg_module.load()
    state.init_db()
    startup_checks(cfg)
    gl = GitLabClient(token=cfg.gitlab_token, base_url=cfg.gitlab_url)

    log.info(f"Issue bot started — watching {len(cfg.repos)} repo(s), "
             f"poll every {cfg.poll_interval_seconds}s")
    for r in cfg.repos:
        log.info(f"  {r.full_name}  keywords={r.keywords}")

    shutdown = False

    def _handle_signal(signum, frame):
        nonlocal shutdown
        log.info("Shutdown signal received")
        shutdown = True

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    with ThreadPoolExecutor(max_workers=cfg.max_concurrent_jobs) as pool:
        while not shutdown:
            for repo_cfg in cfg.repos:
                if shutdown:
                    break
                try:
                    since = state.get_last_polled(repo_cfg.full_name)
                    now = datetime.now(timezone.utc).isoformat()
                    log.info(f"Polling {repo_cfg.full_name} (since={since or 'beginning'})")

                    issues = gl.get_open_issues(repo_cfg.full_name, created_after=since)
                    log.info(f"  {len(issues)} issue(s) returned")

                    for issue in issues:
                        num = issue["iid"]
                        if state.is_processed(repo_cfg.full_name, num):
                            continue
                        if state.is_active(repo_cfg.full_name, num):
                            continue
                        if not keyword_match(issue, repo_cfg.keywords):
                            continue
                        log.info(f"  Dispatching job for !{num}: {issue['title']!r}")
                        pool.submit(process_issue, issue, repo_cfg, cfg, gl)

                    state.set_last_polled(repo_cfg.full_name, now)

                except Exception as e:
                    log.exception(f"Error polling {repo_cfg.full_name}: {e}")

            cleanup_resolved_issues(gl)

            if not shutdown:
                time.sleep(cfg.poll_interval_seconds)

    log.info("Bot shut down cleanly")


if __name__ == "__main__":
    main()
