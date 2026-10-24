"""Graph manager — load, query, and serve graph data."""

import json
from collections import Counter
from pathlib import Path

from database import Database


class GraphManager:
    def __init__(self, db: Database):
        self.db = db
        self._cache: dict[str, dict] = {}  # "owner/name" → parsed graph data

    # ── data loading ─────────────────────────────────────────

    def _cache_key(self, owner: str, name: str) -> str:
        return f"{owner}/{name}"

    def load_graph(self, owner: str, name: str) -> dict | None:
        """Load graph.json into memory cache."""
        key = self._cache_key(owner, name)
        if key in self._cache:
            return self._cache[key]

        repo = self.db.get_repo(owner, name)
        if not repo or not repo.get("graph_json_path"):
            return None

        path = Path(repo["graph_json_path"])
        if not path.exists():
            return None

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._cache[key] = data
            return data
        except Exception:
            return None

    def invalidate_cache(self, owner: str, name: str):
        self._cache.pop(self._cache_key(owner, name), None)

    # ── stats ────────────────────────────────────────────────

    def get_stats(self, owner: str, name: str) -> dict | None:
        data = self.load_graph(owner, name)
        if not data:
            return None

        nodes = data.get("nodes", [])
        links = data.get("links", [])

        file_types = dict(Counter(n.get("file_type", "unknown") for n in nodes))

        communities = Counter()
        community_names = {}
        for n in nodes:
            c = n.get("community")
            if c is not None:
                communities[c] += 1
                cname = n.get("community_name", "")
                if cname and not cname.startswith("Community "):
                    community_names[c] = cname

        relations = dict(Counter(l.get("relation", "unknown") for l in links))
        confidence = dict(Counter(l.get("confidence", "unknown") for l in links))

        return {
            "node_count": len(nodes),
            "link_count": len(links),
            "community_count": len(communities),
            "file_types": file_types,
            "relations": relations,
            "confidence": confidence,
            "top_communities": [
                {
                    "id": cid,
                    "name": community_names.get(cid, f"Community {cid}"),
                    "count": count,
                }
                for cid, count in communities.most_common(20)
            ],
        }

    # ── D3 visualization data ────────────────────────────────

    def get_d3_data(
        self,
        owner: str,
        name: str,
        max_nodes: int = 500,
        community_filter: int | None = None,
        file_type_filter: str | None = None,
    ) -> dict | None:
        data = self.load_graph(owner, name)
        if not data:
            return None

        nodes = data.get("nodes", [])
        links = data.get("links", [])

        if community_filter is not None:
            nodes = [n for n in nodes if n.get("community") == community_filter]
        if file_type_filter:
            nodes = [n for n in nodes if n.get("file_type") == file_type_filter]

        if len(nodes) > max_nodes and community_filter is None:
            comm_sizes = Counter(n.get("community", -1) for n in nodes)
            top_comms = {c for c, _ in comm_sizes.most_common(20)}
            nodes = [n for n in nodes if n.get("community") in top_comms]

        node_ids = {n["id"] for n in nodes}
        links = [
            l for l in links
            if l.get("source") in node_ids and l.get("target") in node_ids
        ]

        degree = Counter()
        for l in links:
            degree[l["source"]] += 1
            degree[l["target"]] += 1

        return {
            "nodes": [
                {
                    "id": n["id"],
                    "label": n.get("label", n["id"].split("::")[-1]),
                    "community": n.get("community", 0),
                    "community_name": n.get("community_name", ""),
                    "file_type": n.get("file_type", ""),
                    "source_file": n.get("source_file", ""),
                    "source_location": n.get("source_location", ""),
                    "repo": n.get("repo", ""),
                    "degree": degree.get(n["id"], 0),
                }
                for n in nodes
            ],
            "links": [
                {
                    "source": l["source"],
                    "target": l["target"],
                    "relation": l.get("relation", ""),
                }
                for l in links
            ],
            "total_nodes": len(data.get("nodes", [])),
            "filtered_nodes": len(nodes),
        }

    # ── god nodes ────────────────────────────────────────────

    def get_god_nodes(self, owner: str, name: str, top_n: int = 10) -> list[dict]:
        data = self.load_graph(owner, name)
        if not data:
            return []

        nodes = data.get("nodes", [])
        links = data.get("links", [])

        degree = Counter()
        for l in links:
            degree[l["source"]] += 1
            degree[l["target"]] += 1

        ranked = sorted(nodes, key=lambda n: degree.get(n["id"], 0), reverse=True)

        result = []
        for n in ranked[:top_n]:
            result.append({
                "id": n["id"],
                "label": n.get("label", ""),
                "degree": degree.get(n["id"], 0),
                "community": n.get("community", 0),
                "community_name": n.get("community_name", ""),
                "source_file": n.get("source_file", ""),
                "file_type": n.get("file_type", ""),
            })
        return result

    # ── node detail ──────────────────────────────────────────

    def get_node_detail(self, owner: str, name: str, node_id: str) -> dict | None:
        data = self.load_graph(owner, name)
        if not data:
            return None

        nodes = data.get("nodes", [])
        links = data.get("links", [])

        node = None
        for n in nodes:
            if n["id"] == node_id:
                node = n
                break
        if not node:
            for n in nodes:
                if n.get("label") == node_id:
                    node = n
                    break
        if not node:
            return None

        neighbor_ids = set()
        node_links = []
        for l in links:
            if l["source"] == node["id"] or l["target"] == node["id"]:
                neighbor_ids.add(l["source"])
                neighbor_ids.add(l["target"])
                node_links.append({
                    "source": l["source"],
                    "target": l["target"],
                    "relation": l.get("relation", ""),
                })

        neighbor_ids.discard(node["id"])
        neighbors = [n for n in nodes if n["id"] in neighbor_ids]

        return {
            "node": {
                "id": node["id"],
                "label": node.get("label", ""),
                "community": node.get("community", 0),
                "community_name": node.get("community_name", ""),
                "source_file": node.get("source_file", ""),
                "source_location": node.get("source_location", ""),
                "file_type": node.get("file_type", ""),
                "repo": node.get("repo", ""),
                "origin": node.get("_origin", ""),
            },
            "neighbors": [
                {
                    "id": n["id"],
                    "label": n.get("label", ""),
                    "community_name": n.get("community_name", ""),
                    "source_file": n.get("source_file", ""),
                }
                for n in neighbors[:50]
            ],
            "links": node_links,
        }

    # ── communities ──────────────────────────────────────────

    def get_communities(self, owner: str, name: str) -> list[dict]:
        data = self.load_graph(owner, name)
        if not data:
            return []

        nodes = data.get("nodes", [])
        comm_counts = Counter()
        comm_names = {}

        for n in nodes:
            c = n.get("community")
            if c is not None:
                comm_counts[c] += 1
                cname = n.get("community_name", "")
                if cname and not cname.startswith("Community "):
                    comm_names[c] = cname

        result = []
        for cid, count in comm_counts.most_common():
            result.append({
                "id": cid,
                "name": comm_names.get(cid, f"Community {cid}"),
                "count": count,
            })
        return result
