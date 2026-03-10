"""
Stage 2 test: verify Claude reads the codebase and makes edits scoped to coffea4bees/.
Gives Claude a trivial real task, checks a file was modified, then restores it.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

CLAUDE_BIN = Path.home() / ".local/bin/claude"
BARISTA_DIR = Path.home() / "issue-bot/worktrees/bot-explore/barista"
COFFEA4BEES_DIR = BARISTA_DIR / "coffea4bees"

PROMPT = """You are being invoked headlessly by the automated issue-bot on falcon.phys.cmu.edu.

You are working inside a git repository. A GitLab issue has been filed that needs fixing.

Issue title: bot:fix: add a module-level docstring to analysis/helpers/cutflow.py
Issue URL: https://gitlab.cern.ch/cms-cmu/coffea4bees/-/issues/999

Issue description:
The file coffea4bees/analysis/helpers/cutflow.py has no module-level docstring.
Please add a brief docstring at the top of the file describing what the module does.

You are running from the `barista/` directory.
The issue is filed against `coffea4bees`, which is located at `coffea4bees/`.
Make all code edits inside `coffea4bees/` unless the issue explicitly involves `barista` itself.

Instructions:
- Investigate the codebase and implement a fix for the issue described above.
- Make all necessary edits to source files.
- Do NOT run git commands, open MRs, or push branches — that is handled externally.
- If the issue is ambiguous, make changes using your best judgement rather than doing nothing.
- When done, briefly summarize what you changed and why, and list any judgement calls you made.
"""


def extract_result(stream_json: str) -> tuple[str | None, str | None]:
    """Return (session_id, result_text) from stream-json output."""
    session_id = None
    result_text = None
    for line in stream_json.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if sid := obj.get("session_id"):
                session_id = sid
            if obj.get("type") == "result" and not obj.get("is_error"):
                result_text = obj.get("result", "")
        except json.JSONDecodeError:
            continue
    return session_id, result_text


def git_status(cwd: Path) -> str:
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(cwd), capture_output=True, text=True
    )
    return result.stdout.strip()


def git_restore(cwd: Path):
    subprocess.run(["git", "checkout", "--", "."], cwd=str(cwd), capture_output=True)


def run():
    print(f"Claude binary  : {CLAUDE_BIN}")
    print(f"Working dir    : {BARISTA_DIR}")
    print(f"coffea4bees    : {COFFEA4BEES_DIR}")
    print()

    for path in [CLAUDE_BIN, BARISTA_DIR, COFFEA4BEES_DIR]:
        if not path.exists():
            print(f"FAIL: {path} not found")
            sys.exit(1)
    print("OK  all paths exist")

    # Snapshot dirty files before test so we only flag new changes
    git_restore(COFFEA4BEES_DIR)
    before_coffea = set(git_status(COFFEA4BEES_DIR).splitlines())
    before_barista = set(git_status(BARISTA_DIR).splitlines())
    if before_coffea:
        print(f"WARN: coffea4bees has pre-existing changes:\n{chr(10).join(before_coffea)}")
    if before_barista - before_coffea:
        print(f"INFO: barista has pre-existing changes (will be ignored):\n{chr(10).join(before_barista)}")

    print("\nInvoking Claude with file-edit task...")
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    result = subprocess.run(
        [str(CLAUDE_BIN), "-p", PROMPT,
         "--output-format", "stream-json",
         "--verbose",
         "--dangerously-skip-permissions"],
        cwd=str(BARISTA_DIR),
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )

    print(f"Exit code: {result.returncode}")
    if result.returncode != 0:
        print(f"FAIL: Claude exited {result.returncode}")
        print("stderr:", result.stderr[:500])
        sys.exit(1)
    print("OK  Claude exited 0")

    session_id, result_text = extract_result(result.stdout)
    print(f"OK  session_id = {session_id}")
    print(f"\nClaude response:\n{result_text}\n")

    # Check that changes were made inside coffea4bees/ only
    after_coffea = set(git_status(COFFEA4BEES_DIR).splitlines())
    after_barista = set(git_status(BARISTA_DIR).splitlines())

    new_coffea = after_coffea - before_coffea
    new_barista_only = {
        l for l in (after_barista - before_barista)
        if "coffea4bees" not in l
    }

    if new_coffea:
        print(f"OK  new changes inside coffea4bees/:\n" + "\n".join(new_coffea))
    else:
        print("FAIL: Claude made no new changes inside coffea4bees/")
        sys.exit(1)

    if new_barista_only:
        print(f"FAIL: new changes outside coffea4bees/ (in barista):\n" + "\n".join(new_barista_only))
        git_restore(COFFEA4BEES_DIR)
        sys.exit(1)
    print("OK  no new changes outside coffea4bees/")

    # Restore
    git_restore(COFFEA4BEES_DIR)
    print("OK  coffea4bees restored to clean state")

    print("\nStage 2 PASSED")


if __name__ == "__main__":
    run()
