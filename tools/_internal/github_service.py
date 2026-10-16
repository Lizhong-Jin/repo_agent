"""GitHub REST broker. No Git config, credential helpers, hooks or host subprocesses."""

import base64
import json
from urllib.parse import quote

from host_support.cancellation import checkpoint

from .dependency_service import read_workspace, sha256


class GitHubService:
    def __init__(self, http, token=None):
        if token is not None and (
            not token or len(token) > 4096 or any(not 33 <= ord(c) <= 126 for c in token)
        ):
            raise ValueError("Invalid GitHub token")
        self.http, self._token = http, token

    def api(self, repo, suffix="", *, method="GET", payload=None):
        checkpoint()
        result = self.http.request(
            method,
            f"https://api.github.com/repos/{repo}{suffix}",
            payload=payload,
            token=self._token,
            limit=2 * 1024**2,
        )
        return json.loads(result["body"])

    def read(self, scope):
        operation = scope["operation"]
        number = scope.get("number")
        suffix = {
            "repository": "",
            "pulls": "/pulls?per_page=30",
            "runs": "/actions/runs?per_page=30",
            "branches": "/branches?per_page=30",
            "pull": f"/pulls/{number}",
            "run": f"/actions/runs/{number}",
            "jobs": f"/actions/runs/{number}/jobs?per_page=30",
        }[operation]
        data = json.dumps(self.api(scope["repo"], suffix), ensure_ascii=True)
        if self._token:
            data = data.replace(self._token, "[REDACTED]")
        return {"untrusted": True, "content": data[:24000], "truncated": len(data) > 24000}

    def write(self, scope, access):
        if not self._token:
            raise ValueError("A host-provided REPO_AGENT_GITHUB_TOKEN is required")
        repo = scope["repo"]
        if scope["operation"] == "create_pull_request":
            data = self.api(
                repo,
                "/pulls",
                method="POST",
                payload={**{k: scope[k] for k in ("head", "base", "title", "body")}, "draft": True},
            )
            return {"number": data["number"], "url": data["html_url"], "draft": True}
        # Materialize and verify every approved byte before the first upload.
        files = []
        for record in scope["files"]:
            raw = read_workspace(access, record["path"], 1024**2)
            if len(raw) != record["size"] or sha256(raw) != record["sha256"]:
                raise ValueError("Approved publication changed")
            files.append((record["path"], raw))
        reference = "/git/ref/heads/" + quote(scope["branch"], safe="/")
        base = scope["expected_sha"]
        if self.api(repo, reference)["object"]["sha"] != base:
            raise ValueError("Remote branch advanced; prepare a new approval")
        commit = self.api(repo, "/git/commits/" + base)
        tree = []
        for path, raw in files:
            blob = self.api(
                repo,
                "/git/blobs",
                method="POST",
                payload={"content": base64.b64encode(raw).decode(), "encoding": "base64"},
            )
            tree.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        created_tree = self.api(
            repo,
            "/git/trees",
            method="POST",
            payload={"base_tree": commit["tree"]["sha"], "tree": tree},
        )
        created = self.api(
            repo,
            "/git/commits",
            method="POST",
            payload={"message": scope["message"], "tree": created_tree["sha"], "parents": [base]},
        )
        if self.api(repo, reference)["object"]["sha"] != base:
            raise ValueError("Remote branch advanced; no reference was updated")
        self.api(
            repo,
            "/git/refs/heads/" + quote(scope["branch"], safe="/"),
            method="PATCH",
            payload={"sha": created["sha"], "force": False},
        )
        return {
            "commit": created["sha"],
            "branch": scope["branch"],
            "files": [p for p, _ in files],
            "force": False,
            "note": "Published an approved file snapshot through GitHub; "
            "local Git history was not pushed or modified.",
        }
