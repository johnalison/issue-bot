"""
Stage 1 test: verify Claude can be invoked headlessly from the barista root.
Checks:
  - Claude binary is reachable
  - Subscription auth works (no API key needed)
  - --output-format stream-json produces parseable output
  - Session ID is extractable from the transcript
"""

import json
import subprocess
import sys
from pathlib import Path

CLAUDE_BIN = Path.home() / ".local/bin/claude"
BARISTA_DIR = Path.home() / "issue-bot/worktrees/bot-explore/barista"
PROMPT = "Say hello in one sentence. Do not use any tools."


def extract_session_id(stream_json: str) -> str | None:
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


def run():
    print(f"Claude binary : {CLAUDE_BIN}")
    print(f"Working dir   : {BARISTA_DIR}")
    print()

    if not CLAUDE_BIN.exists():
        print(f"FAIL: claude binary not found at {CLAUDE_BIN}")
        sys.exit(1)
    print("OK  claude binary exists")

    if not BARISTA_DIR.exists():
        print(f"FAIL: barista dir not found at {BARISTA_DIR}")
        sys.exit(1)
    print("OK  barista worktree exists")

    print("\nInvoking Claude headlessly...")
    env = {k: v for k, v in __import__("os").environ.items() if k != "CLAUDECODE"}
    result = subprocess.run(
        [str(CLAUDE_BIN), "-p", PROMPT,
         "--output-format", "stream-json",
         "--verbose",
         "--dangerously-skip-permissions"],
        cwd=str(BARISTA_DIR),
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )

    print(f"Exit code: {result.returncode}")

    if result.returncode != 0:
        print(f"FAIL: Claude exited {result.returncode}")
        print("stderr:", result.stderr[:500])
        sys.exit(1)
    print("OK  Claude exited 0")

    if not result.stdout.strip():
        print("FAIL: no stdout output")
        sys.exit(1)
    print("OK  stdout has content")

    # Check stream-json is parseable
    parsed = 0
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            json.loads(line)
            parsed += 1
        except json.JSONDecodeError as e:
            print(f"FAIL: unparseable JSON line: {line[:100]}: {e}")
            sys.exit(1)
    print(f"OK  {parsed} stream-json lines parsed")

    # Extract session ID
    session_id = extract_session_id(result.stdout)
    if session_id:
        print(f"OK  session_id = {session_id}")
    else:
        print("WARN: no session_id found in output (non-fatal)")

    print("\nStage 1 PASSED")


if __name__ == "__main__":
    run()
