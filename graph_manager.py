"""Graph manager — load, query, and serve graph data."""

import json
from collections import Counter
from pathlib import Path

from database import Database


class GraphManager:
    def __init__(self, db: Database):
        self.db = db
        self._cache: dict[str, dict] = {}  # graph_id → parsed graph data

    # ── data loading ─────────────────────────────────────────

    def load_graph(self, graph_id: str) -> dict | None:
        """Load graph.json into memory cache."""
        if graph_id in self._cache:
            return self._cache[graph_id]

        graph = self.db.get_graph(graph_id)
        if not graph:
            return None

        path = Path(graph["graph_json_path"])
        if not path.exists():
            return None

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._cache[graph_id] = data
            return data
        except Exception:
            return None

    def invalidate_cache(self, graph_id: str):
        self._cache.pop(graph_id, None)

    # ── stats ────────────────────────────────────────────────

    def get_stats(self, graph_id: str) -> dict | None:
        data = self.load_graph(graph_id)
        if not data:
            return None

        nodes = data.get("nodes", [])
        links = data.get("links", [])

        # File type distribution
        file_types = dict(Counter(n.get("file_type", "unknown") for n in nodes))

        # Community distribution
        communities = Counter()
        community_names = {}
        for n in nodes:
            c = n.get("community")
            if c is not None:
                communities[c] += 1
                name = n.get("community_name", "")
                if name and not name.startswith("Community "):
                    community_names[c] = name

        # Edge relation distribution
        relations = dict(Counter(l.get("relation", "unknown") for l in links))

        # Confidence distribution
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
        graph_id: str,
        max_nodes: int = 500,
        community_filter: int | None = None,
        file_type_filter: str | None = None,
    ) -> dict | None:
        data = self.load_graph(graph_id)
        if not data:
            return None

        nodes = data.get("nodes", [])
        links = data.get("links", [])

        # Apply filters
        if community_filter is not None:
            nodes = [n for n in nodes if n.get("community") == community_filter]
        if file_type_filter:
            nodes = [n for n in nodes if n.get("file_type") == file_type_filter]

        # If too many nodes, keep top communities
        if len(nodes) > max_nodes and community_filter is None:
            comm_sizes = Counter(n.get("community", -1) for n in nodes)
            top_comms = {c for c, _ in comm_sizes.most_common(20)}
            nodes = [n for n in nodes if n.get("community") in top_comms]

        # Build node ID set for link filtering
        node_ids = {n["id"] for n in nodes}
        links = [
            l for l in links
            if l.get("source") in node_ids and l.get("target") in node_ids
        ]

        # Compute degree
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

    def get_god_nodes(self, graph_id: str, top_n: int = 10) -> list[dict]:
        data = self.load_graph(graph_id)
        if not data:
            return []

        nodes = data.get("nodes", [])
        links = data.get("links", [])

        degree = Counter()
        for l in links:
            degree[l["source"]] += 1
            degree[l["target"]] += 1

        # Sort by degree
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

    def get_node_detail(self, graph_id: str, node_id: str) -> dict | None:
        data = self.load_graph(graph_id)
        if not data:
            return None

        nodes = data.get("nodes", [])
        links = data.get("links", [])

        # Find the node
        node = None
        for n in nodes:
            if n["id"] == node_id:
                node = n
                break
        if not node:
            # Try by label
            for n in nodes:
                if n.get("label") == node_id:
                    node = n
                    break
        if not node:
            return None

        # Find neighbors
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

    def get_communities(self, graph_id: str) -> list[dict]:
        data = self.load_graph(graph_id)
        if not data:
            return []

        nodes = data.get("nodes", [])
        comm_counts = Counter()
        comm_names = {}

        for n in nodes:
            c = n.get("community")
            if c is not None:
                comm_counts[c] += 1
                name = n.get("community_name", "")
                if name and not name.startswith("Community "):
                    comm_names[c] = name

        result = []
        for cid, count in comm_counts.most_common():
            result.append({
                "id": cid,
                "name": comm_names.get(cid, f"Community {cid}"),
                "count": count,
            })
        return result

    # ── cleanup ──────────────────────────────────────────────

    def delete_graph(self, graph_id: str) -> dict:
        graph = self.db.get_graph(graph_id)
        if not graph:
            return {"error": "图谱不存在"}

        # Delete file
        path = Path(graph["graph_json_path"])
        if path.exists():
            path.unlink(missing_ok=True)

        self.invalidate_cache(graph_id)
        self.db.delete_graph(graph_id)
        return {"ok": True, "name": graph["name"]}
