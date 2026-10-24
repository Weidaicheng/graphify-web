"""Repository manager — clone repos and run graphify pipeline."""

import asyncio
import json
import shutil
from pathlib import Path

from database import Database
from github_api import GitHubAPI


class RepoManager:
    def __init__(self, db: Database, repos_dir: Path, graphify_bin: str):
        self.db = db
        self.repos_dir = repos_dir
        self.graphify_bin = graphify_bin
        self.repos_dir.mkdir(parents=True, exist_ok=True)

    # ── public API ───────────────────────────────────────────

    async def clone_and_build(self, owner: str, name: str) -> dict:
        """Full pipeline: clone repo → build graph. Returns repo dict."""
        github_url = f"https://github.com/{owner}/{name}"
        local_path = str(self.repos_dir / f"{owner}_{name}")

        # Create or reset repo record
        repo = self.db.add_repo(owner=owner, name=name, local_path=local_path)

        # Get latest commit SHA before cloning
        commit_sha = await GitHubAPI.get_latest_commit(owner, name)

        # Clone
        job = self.db.add_job(owner, name, "clone")
        rc, output = await self._run_graphify(
            [self.graphify_bin, "clone", github_url, "--out", local_path],
            job["id"],
        )
        if rc != 0:
            self.db.update_repo(owner, name, status="error", error_message=f"克隆失败: {output[-500:]}")
            self.db.update_job(job["id"], status="failed", finished_at=Database._now())
            return {"error": f"克隆失败: {output[-500:]}", "repo": repo}

        self.db.update_job(job["id"], status="completed", finished_at=Database._now())
        self.db.update_repo(owner, name, status="ready", local_path=local_path)

        # Build graph
        return await self.build_graph(owner, name, commit_sha=commit_sha)

    async def build_graph(self, owner: str, name: str, commit_sha: str = None) -> dict:
        """Run full graphify pipeline on a cloned repo."""
        repo = self.db.get_repo(owner, name)
        if not repo:
            return {"error": "仓库不存在"}
        if repo["status"] == "building":
            return {"error": "仓库正在构建中"}

        self.db.update_repo(owner, name, status="building", error_message=None)

        # Create job
        job = self.db.add_job(owner, name, "build")

        # Run graphify pipeline
        rc, output = await self._run_graphify(
            [self.graphify_bin, repo["local_path"]],
            job["id"],
        )

        if rc != 0:
            self.db.update_repo(owner, name, status="error", error_message=output[-500:])
            self.db.update_job(job["id"], status="failed", finished_at=Database._now())
            return {"error": f"构建失败: {output[-500:]}"}

        self.db.update_job(job["id"], status="completed", finished_at=Database._now())

        # Find graph.json in graphify-out directory
        graph_json = Path(repo["local_path"]) / "graphify-out" / "graph.json"
        if not graph_json.exists():
            for candidate in [
                Path(repo["local_path"]) / "graph.json",
                Path("graphify-out") / "graph.json",
            ]:
                if candidate.exists():
                    graph_json = candidate
                    break

        if not graph_json.exists():
            self.db.update_repo(owner, name, status="error", error_message="graph.json not found")
            return {"error": "构建完成但未找到 graph.json"}

        # Parse stats and update repo record
        stats = self._parse_graph_stats(graph_json)

        # Get commit SHA if not provided
        if commit_sha is None:
            commit_sha = await GitHubAPI.get_latest_commit(owner, name)

        self.db.update_repo(
            owner, name,
            status="ready",
            graph_json_path=str(graph_json),
            node_count=stats["node_count"],
            link_count=stats["link_count"],
            community_count=stats["community_count"],
            last_commit_sha=commit_sha,
            last_built_sha=commit_sha,
            error_message=None,
        )

        return self.db.get_repo(owner, name)

    async def update_graph(self, owner: str, name: str) -> dict:
        """Incremental update: graphify --update."""
        repo = self.db.get_repo(owner, name)
        if not repo:
            return {"error": "仓库不存在"}
        if not repo.get("graph_json_path"):
            return await self.build_graph(owner, name)

        self.db.update_repo(owner, name, status="building", error_message=None)

        job = self.db.add_job(owner, name, "update")
        rc, output = await self._run_graphify(
            [self.graphify_bin, repo["local_path"], "--update"],
            job["id"],
        )

        if rc != 0:
            self.db.update_repo(owner, name, status="error", error_message=output[-500:])
            self.db.update_job(job["id"], status="failed", finished_at=Database._now())
            return {"error": f"更新失败: {output[-500:]}"}

        self.db.update_job(job["id"], status="completed", finished_at=Database._now())

        # Re-parse stats
        graph_json = Path(repo["graph_json_path"])
        if graph_json.exists():
            stats = self._parse_graph_stats(graph_json)
        else:
            stats = {"node_count": 0, "link_count": 0, "community_count": 0}

        # Get latest commit SHA
        commit_sha = await GitHubAPI.get_latest_commit(owner, name)

        self.db.update_repo(
            owner, name,
            status="ready",
            node_count=stats["node_count"],
            link_count=stats["link_count"],
            community_count=stats["community_count"],
            last_commit_sha=commit_sha,
            last_built_sha=commit_sha,
            error_message=None,
        )
        return self.db.get_repo(owner, name)

    async def delete_repo(self, owner: str, name: str) -> dict:
        """Delete repo local files and DB record."""
        repo = self.db.get_repo(owner, name)
        if not repo:
            return {"error": "仓库不存在"}

        # Delete local files
        local_path = Path(repo["local_path"])
        if local_path.exists():
            shutil.rmtree(local_path, ignore_errors=True)

        self.db.delete_repo(owner, name)
        return {"ok": True, "owner": owner, "name": name}

    # ── internals ────────────────────────────────────────────

    async def _run_graphify(self, cmd: list[str], job_id: str) -> tuple[int, str]:
        """Run graphify subprocess, stream output to job log."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(self.repos_dir),
            )
        except FileNotFoundError:
            return 1, f"graphify binary not found: {cmd[0]}"

        output_lines = []
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace")
            output_lines.append(text)
            if len(output_lines) % 10 == 0:
                self.db.update_job(job_id, log_output="".join(output_lines))

        rc = await proc.wait()
        final_output = "".join(output_lines)
        self.db.update_job(job_id, log_output=final_output)
        return rc, final_output

    @staticmethod
    def _parse_graph_stats(graph_json_path: Path) -> dict:
        """Load graph.json and count nodes/links/communities."""
        try:
            with open(graph_json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            nodes = data.get("nodes", [])
            links = data.get("links", [])
            communities = set()
            for n in nodes:
                c = n.get("community")
                if c is not None:
                    communities.add(c)
            return {
                "node_count": len(nodes),
                "link_count": len(links),
                "community_count": len(communities),
            }
        except Exception:
            return {"node_count": 0, "link_count": 0, "community_count": 0}
