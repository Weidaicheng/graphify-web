"""Repository manager — clone repos and run graphify pipeline."""

import asyncio
import json
import re
import shutil
from collections import Counter
from pathlib import Path

from database import Database


class RepoManager:
    def __init__(self, db: Database, repos_dir: Path, graphify_bin: str):
        self.db = db
        self.repos_dir = repos_dir
        self.graphify_bin = graphify_bin
        self.repos_dir.mkdir(parents=True, exist_ok=True)

    # ── public API ───────────────────────────────────────────

    async def clone_and_build(self, github_url: str) -> dict:
        """Full pipeline: clone repo → build graph. Returns repo dict."""
        # Check duplicate
        existing = self.db.get_repo_by_url(github_url)
        if existing:
            return {"error": f"仓库已存在: {existing['name']}", "repo": existing}

        name = self._extract_repo_name(github_url)
        local_path = str(self.repos_dir / name.replace("/", "_"))

        # Create repo record
        repo = self.db.add_repo(name=name, github_url=github_url, local_path=local_path)

        # Clone
        job = self.db.add_job(repo["id"], "clone")
        rc, output = await self._run_graphify(
            [self.graphify_bin, "clone", github_url, "--out", local_path],
            job["id"],
        )
        if rc != 0:
            self.db.update_repo(repo["id"], status="error")
            self.db.update_job(job["id"], status="failed", finished_at=Database._now())
            return {"error": f"克隆失败: {output[-500:]}", "repo": repo}

        self.db.update_job(job["id"], status="completed", finished_at=Database._now())
        self.db.update_repo(repo["id"], status="ready", local_path=local_path)

        # Build graph
        return await self.build_graph(repo["id"])

    async def build_graph(self, repo_id: str) -> dict:
        """Run full graphify pipeline on a cloned repo."""
        repo = self.db.get_repo(repo_id)
        if not repo:
            return {"error": "仓库不存在"}
        if repo["status"] == "cloning":
            return {"error": "仓库正在克隆中"}

        self.db.update_repo(repo_id, status="building")

        # Create graph record
        graph = self.db.add_graph(
            repo_id=repo_id,
            name=repo["name"],
            graph_json_path="",  # will be updated after build
        )
        self.db.update_graph(graph["id"], status="building")

        # Create job
        job = self.db.add_job(repo_id, "build")

        # Run graphify pipeline
        rc, output = await self._run_graphify(
            [self.graphify_bin, repo["local_path"]],
            job["id"],
        )

        if rc != 0:
            self.db.update_repo(repo_id, status="error")
            self.db.update_graph(graph["id"], status="error", error_message=output[-500:])
            self.db.update_job(job["id"], status="failed", finished_at=Database._now())
            return {"error": f"构建失败: {output[-500:]}", "graph": graph}

        self.db.update_job(job["id"], status="completed", finished_at=Database._now())

        # Find graph.json in graphify-out directory
        graph_json = Path(repo["local_path"]) / "graphify-out" / "graph.json"
        if not graph_json.exists():
            # Try alternative locations
            for candidate in [
                Path(repo["local_path"]) / "graph.json",
                Path("graphify-out") / "graph.json",
            ]:
                if candidate.exists():
                    graph_json = candidate
                    break

        if not graph_json.exists():
            self.db.update_graph(graph["id"], status="error", error_message="graph.json not found")
            self.db.update_repo(repo_id, status="error")
            return {"error": "构建完成但未找到 graph.json"}

        # Parse stats and update graph record
        stats = self._parse_graph_stats(graph_json)
        self.db.update_graph(
            graph["id"],
            status="ready",
            graph_json_path=str(graph_json),
            node_count=stats["node_count"],
            link_count=stats["link_count"],
            community_count=stats["community_count"],
        )
        self.db.update_repo(repo_id, status="ready")

        return self.db.get_graph(graph["id"])

    async def update_graph(self, repo_id: str) -> dict:
        """Incremental update: graphify --update."""
        repo = self.db.get_repo(repo_id)
        if not repo:
            return {"error": "仓库不存在"}

        graphs = self.db.list_graphs_by_repo(repo_id)
        if not graphs:
            return await self.build_graph(repo_id)

        graph = graphs[0]  # latest graph
        self.db.update_graph(graph["id"], status="building")

        job = self.db.add_job(repo_id, "update")
        rc, output = await self._run_graphify(
            [self.graphify_bin, repo["local_path"], "--update"],
            job["id"],
        )

        if rc != 0:
            self.db.update_graph(graph["id"], status="error", error_message=output[-500:])
            self.db.update_job(job["id"], status="failed", finished_at=Database._now())
            return {"error": f"更新失败: {output[-500:]}"}

        self.db.update_job(job["id"], status="completed", finished_at=Database._now())

        # Re-parse stats
        graph_json = Path(graph["graph_json_path"])
        if graph_json.exists():
            stats = self._parse_graph_stats(graph_json)
            self.db.update_graph(
                graph["id"],
                status="ready",
                node_count=stats["node_count"],
                link_count=stats["link_count"],
                community_count=stats["community_count"],
            )

        self.db.update_repo(repo_id, status="ready")
        return self.db.get_graph(graph["id"])

    async def delete_repo(self, repo_id: str) -> dict:
        """Delete repo, associated graphs, and local files."""
        repo = self.db.get_repo(repo_id)
        if not repo:
            return {"error": "仓库不存在"}

        # Delete local files
        local_path = Path(repo["local_path"])
        if local_path.exists():
            shutil.rmtree(local_path, ignore_errors=True)

        # Delete graph files
        for graph in self.db.list_graphs_by_repo(repo_id):
            graph_path = Path(graph["graph_json_path"])
            if graph_path.exists():
                graph_path.unlink(missing_ok=True)
            self.db.delete_graph(graph["id"])

        self.db.delete_repo(repo_id)
        return {"ok": True, "name": repo["name"]}

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
            # Update job log incrementally (every 10 lines to reduce DB writes)
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
        except Exception as e:
            return {"node_count": 0, "link_count": 0, "community_count": 0}

    @staticmethod
    def _extract_repo_name(github_url: str) -> str:
        """Extract 'owner/repo' from a GitHub URL."""
        # Handle: https://github.com/owner/repo.git, https://github.com/owner/repo
        m = re.search(r"github\.com[:/]([^/]+/[^/.]+?)(?:\.git)?$", github_url)
        if m:
            return m.group(1)
        # Fallback: use last two path segments
        parts = github_url.rstrip("/").split("/")
        if len(parts) >= 2:
            return f"{parts[-2]}/{parts[-1].replace('.git', '')}"
        return github_url
