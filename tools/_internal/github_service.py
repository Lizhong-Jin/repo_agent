"""GitHub REST broker. No Git config, credential helpers, hooks or host subprocesses."""

import base64
import json
from urllib.parse import quote, urlencode

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

    def write(self, scope, access, *, journal, record):
        if not self._token:
            raise ValueError("A host-provided REPO_AGENT_GITHUB_TOKEN is required")
        repo = scope["repo"]
        if scope["operation"] == "create_pull_request":
            journal.update(record, stage="mutation_requested")
            data = self.api(
                repo,
                "/pulls",
                method="POST",
                payload={**{k: scope[k] for k in ("head", "base", "title", "body")}, "draft": True},
            )
            number = data["number"]
            if type(number) is not int or number < 1:
                raise ValueError("Invalid GitHub PR response")
            journal.update(record, stage="response_received", number=number)
            return {
                "number": number,
                "url": f"https://github.com/{repo}/pull/{number}",
                "draft": True,
            }
        # Materialize and verify every approved byte before the first upload.
        files = []
        for file in scope["files"]:
            raw = read_workspace(access, file["path"], 1024**2)
            if len(raw) != file["size"] or sha256(raw) != file["sha256"]:
                raise ValueError("Approved publication changed")
            files.append((file["path"], raw))
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
        from tools.service_scopes import digest

        digest(created["sha"], 40)
        journal.update(record, stage="objects_created", candidate_commit=created["sha"])
        if self.api(repo, reference)["object"]["sha"] != base:
            raise ValueError("Remote branch advanced; no reference was updated")
        journal.update(record, stage="mutation_requested")
        updated = self.api(
            repo,
            "/git/refs/heads/" + quote(scope["branch"], safe="/"),
            method="PATCH",
            payload={"sha": created["sha"], "force": False},
        )
        if (
            updated.get("ref") != "refs/heads/" + scope["branch"]
            or updated.get("object", {}).get("sha") != created["sha"]
            or updated.get("object", {}).get("type") != "commit"
        ):
            raise ValueError("GitHub branch update response does not confirm the approved commit")
        journal.update(record, stage="response_received")
        return {
            "commit": created["sha"],
            "branch": scope["branch"],
            "files": [p for p, _ in files],
            "force": False,
            "note": "Published an approved file snapshot through GitHub; "
            "local Git history was not pushed or modified.",
        }

    def verify(self, record):
        """Only GETs; negative/partial observations never authorize a repeated write."""
        scope, stage = record["scope"], record["stage"]
        repo = scope["repo"]
        if stage in {"prepared", "objects_created"}:
            return {
                "state": "not_submitted",
                "reason": "未提交发布/创建请求；可能存在未引用 Git 对象",
            }
        if scope["operation"] == "publish_files":
            candidate = record.get("candidate_commit")
            if not candidate:
                return {"state": "unknown", "reason": "缺少候选提交标识"}
            reference = "/git/ref/heads/" + quote(scope["branch"], safe="/")
            head = self.api(repo, reference)["object"]["sha"]
            from tools.service_scopes import digest

            digest(head, 40)
            if head == candidate:
                return {
                    "state": "confirmed",
                    "commit": candidate,
                    "head": head,
                    "reason": "目标分支当前指向已记录的候选提交",
                }
            comparison = self.api(repo, f"/compare/{candidate}...{head}?per_page=1")
            # The compare status proves ancestry without enumerating/truncating commits.
            if (
                comparison.get("status") == "ahead"
                and comparison.get("merge_base_commit", {}).get("sha") == candidate
            ):
                # Pin the observed head; a concurrent force push makes the observation unknown.
                if self.api(repo, reference)["object"]["sha"] == head:
                    return {
                        "state": "confirmed",
                        "commit": candidate,
                        "head": head,
                        "reason": "目标分支当前包含已记录候选提交；不证明当前文件内容未被后续修改",
                    }
            return {
                "state": "unknown",
                "head": head,
                "reason": "当前未确认候选提交在目标分支上；不能证明此前从未发布",
            }
        number = record.get("number")
        if number is not None:
            data = self.api(repo, f"/pulls/{number}")
            if (
                data.get("number") == number
                and data.get("base", {}).get("repo", {}).get("full_name", "").casefold()
                == repo.casefold()
            ):
                return {
                    "state": "confirmed",
                    "number": number,
                    "url": f"https://github.com/{repo}/pull/{number}",
                    "reason": "已找到创建响应中保存的 PR；内容或状态可能已被后续修改",
                }
            return {"state": "unknown", "reason": "PR 响应与已记录身份不匹配"}
        query = urlencode(
            {
                "state": "all",
                "head": repo.split("/")[0] + ":" + scope["head"],
                "base": scope["base"],
                "per_page": 30,
            }
        )
        rows = self.api(repo, "/pulls?" + query)
        if not isinstance(rows, list):
            raise ValueError("Invalid PR query response")
        candidates = [
            row["number"]
            for row in rows[:30]
            if type(row.get("number")) is int
            and row["number"] > 0
            and row.get("title") == scope["title"]
            and (row.get("body") or "") == scope["body"]
        ]
        return {
            "state": "unknown",
            "candidates": candidates,
            "reason": "创建响应未保存 PR 标识；相似 PR 仅供人工核实，首屏未找到也不代表未创建",
        }
