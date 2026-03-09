"""
Config loading: reads config.yaml and overlays env vars.
"""

import os
import yaml
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class DepConfig:
    owner: str
    name: str
    base_branch: str = "master"

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass
class RepoConfig:
    owner: str
    name: str
    keywords: list[str]
    base_branch: str = "master"
    dependencies: list[DepConfig] = field(default_factory=list)
    parent_dep: str | None = None  # clone repo inside this dependency's directory

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass
class Config:
    repos: list[RepoConfig]
    poll_interval_seconds: int
    worktree_base: Path
    log_dir: Path
    max_concurrent_jobs: int
    gitlab_token: str
    anthropic_api_key: str = ""  # unused; auth via claude login
    gitlab_url: str = "https://gitlab.cern.ch"
    claude_timeout_seconds: int = 600
    claude_bin: str = str(Path.home() / ".local/bin/claude")


def load(config_path: str | Path = None) -> "Config":
    config_path = Path(config_path or Path(__file__).parent / "config.yaml")
    with open(config_path) as f:
        raw = yaml.safe_load(f)

    repos = []
    for r in raw["repos"]:
        deps = [
            DepConfig(
                owner=d["owner"],
                name=d["name"],
                base_branch=d.get("base_branch", "master"),
            )
            for d in r.get("dependencies", [])
        ]
        repos.append(RepoConfig(
            owner=r["owner"],
            name=r["name"],
            keywords=r.get("keywords", []),
            base_branch=r.get("base_branch", "master"),
            dependencies=deps,
            parent_dep=r.get("parent_dep"),
        ))

    gitlab_token = os.environ.get("GITLAB_TOKEN") or raw.get("gitlab_token", "")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY") or raw.get("anthropic_api_key", "")

    missing = []
    if not gitlab_token:
        missing.append("GITLAB_TOKEN")
    if missing:
        raise EnvironmentError(f"Missing required credentials: {', '.join(missing)}")

    return Config(
        repos=repos,
        poll_interval_seconds=raw.get("poll_interval_seconds", 300),
        worktree_base=Path(raw.get("worktree_base", "~/issue-bot/worktrees")).expanduser(),
        log_dir=Path(raw.get("log_dir", "~/issue-bot/logs")).expanduser(),
        max_concurrent_jobs=raw.get("max_concurrent_jobs", 2),
        gitlab_token=gitlab_token,
        anthropic_api_key=anthropic_key,
        gitlab_url=raw.get("gitlab_url", "https://gitlab.cern.ch"),
        claude_timeout_seconds=raw.get("claude_timeout_seconds", 600),
        claude_bin=raw.get("claude_bin", str(Path.home() / ".local/bin/claude")),
    )
