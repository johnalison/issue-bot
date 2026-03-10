"""
CERN GitLab REST API client with rate-limit handling.
Supports issues and merge request creation.
"""

import time
import requests
from urllib.parse import quote


class GitLabError(Exception):
    pass


class GitLabClient:
    def __init__(self, token: str, base_url: str = "https://gitlab.cern.ch"):
        self.base_url = base_url.rstrip("/")
        self.api = f"{self.base_url}/api/v4"
        self.session = requests.Session()
        self.session.headers.update({"PRIVATE-TOKEN": token})

    def _encode_path(self, project_path: str) -> str:
        """URL-encode a project path like 'cms-cmu/coffea4bees'."""
        return quote(project_path, safe="")

    def _get(self, path: str, params: dict = None) -> requests.Response:
        url = f"{self.api}{path}"
        while True:
            resp = self.session.get(url, params=params)
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 60))
                print(f"[gitlab] Rate limited — sleeping {retry_after}s")
                time.sleep(retry_after)
                continue
            if not resp.ok:
                raise GitLabError(f"GET {url} → {resp.status_code}: {resp.text[:300]}")
            return resp

    def _post(self, path: str, json: dict) -> requests.Response:
        url = f"{self.api}{path}"
        resp = self.session.post(url, json=json)
        if not resp.ok:
            raise GitLabError(f"POST {url} → {resp.status_code}: {resp.text[:300]}")
        return resp

    def get_project_id(self, project_path: str) -> int:
        """Resolve a project path to its numeric ID (cached by caller if needed)."""
        resp = self._get(f"/projects/{self._encode_path(project_path)}")
        return resp.json()["id"]

    def get_open_issues(self, project_path: str, created_after: str = None) -> list[dict]:
        """Return open issues, optionally filtered to those created after a timestamp."""
        params = {
            "state": "opened",
            "order_by": "created_at",
            "sort": "desc",
            "per_page": 50,
        }
        if created_after:
            params["created_after"] = created_after
        encoded = self._encode_path(project_path)
        resp = self._get(f"/projects/{encoded}/issues", params=params)
        return resp.json()

    def get_default_branch(self, project_path: str) -> str:
        resp = self._get(f"/projects/{self._encode_path(project_path)}")
        return resp.json()["default_branch"]

    def create_branch(self, project_path: str, branch: str, ref: str) -> None:
        encoded = self._encode_path(project_path)
        self._post(f"/projects/{encoded}/repository/branches", {
            "branch": branch,
            "ref": ref,
        })

    def create_mr(self, project_path: str, title: str, description: str,
                  source_branch: str, target_branch: str) -> str:
        """Create a merge request and return its URL."""
        encoded = self._encode_path(project_path)
        resp = self._post(f"/projects/{encoded}/merge_requests", {
            "title": title,
            "description": description,
            "source_branch": source_branch,
            "target_branch": target_branch,
            "remove_source_branch": True,
        })
        return resp.json()["web_url"]

    def get_clone_url(self, project_path: str) -> str:
        """SSH clone URL using port 7999 (required for gitlab.cern.ch)."""
        host = self.base_url.replace("https://", "")
        return f"ssh://git@{host}:7999/{project_path}.git"
