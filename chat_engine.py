"""Chat engine — graph-context RAG with LLM streaming."""

import asyncio
import json
from collections import Counter

from graph_manager import GraphManager

# Lazy import httpx to avoid startup errors if not installed
_httpx = None


def _get_httpx():
    global _httpx
    if _httpx is None:
        import httpx
        _httpx = httpx
    return _httpx


class ChatEngine:
    def __init__(self, graph_manager: GraphManager, llm_config: dict, graphify_bin: str):
        self.graph_mgr = graph_manager
        self.base_url = llm_config.get("base_url", "https://api.quickrouter.ai/v1")
        self.api_key = llm_config.get("api_key", "")
        self.model = llm_config.get("model", "gpt-4o-mini")
        self.graphify_bin = graphify_bin

    async def chat_stream(self, graph_id: str, query: str, history: list[dict] = None):
        """
        Generator yielding SSE event dicts:
        {"type": "context", ...} → graph context used
        {"type": "token", "content": "..."} → LLM token
        {"type": "done"} → stream complete
        {"type": "error", "message": "..."} → error
        """
        # 1. Get graph context via subprocess
        graph = self.graph_mgr.db.get_graph(graph_id)
        if not graph:
            yield {"type": "error", "message": "图谱不存在"}
            return

        graph_json_path = graph["graph_json_path"]
        repo_name = graph.get("name", "unknown")

        # Run graphify query
        context_text, context_nodes = await self._query_graph(graph_json_path, query)

        # Get god nodes for architectural context
        god_nodes = self.graph_mgr.get_god_nodes(graph_id, top_n=10)

        # Yield context event
        yield {
            "type": "context",
            "nodes": context_nodes[:20],
            "god_nodes": [g["label"] for g in god_nodes[:5]],
        }

        # 2. Build system prompt
        system_prompt = self._build_system_prompt(
            context_text, god_nodes, repo_name,
        )

        # 3. Build messages
        messages = [{"role": "system", "content": system_prompt}]
        if history:
            for msg in history[-10:]:
                messages.append({"role": msg["role"], "content": msg["content"]})
        messages.append({"role": "user", "content": query})

        # 4. Call LLM with streaming
        if not self.api_key:
            # No API key — return context-only response
            yield {"type": "token", "content": f"⚠️ 未配置 LLM API key，以下是图谱查询结果：\n\n{context_text}"}
            yield {"type": "done"}
            return

        try:
            async for token in self._call_llm_stream(messages):
                yield {"type": "token", "content": token}
            yield {"type": "done"}
        except Exception as e:
            yield {"type": "error", "message": f"LLM 调用失败: {str(e)}"}

    async def _query_graph(self, graph_json_path: str, question: str) -> tuple[str, list[dict]]:
        """Run graphify query and parse output. Returns (raw_text, parsed_nodes)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                self.graphify_bin, "query", question,
                "--graph", graph_json_path,
                "--budget", "3000",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout = await proc.stdout.read()
            text = stdout.decode("utf-8", errors="replace")
            await proc.wait()
        except Exception as e:
            return f"查询失败: {e}", []

        # Parse NODE/EDGE lines
        nodes = []
        for line in text.split("\n"):
            line = line.strip()
            if line.startswith("NODE:"):
                parts = line[5:].strip().split("|")
                if len(parts) >= 2:
                    nodes.append({"label": parts[0].strip(), "info": parts[1].strip()})
            elif line.startswith("EDGE:"):
                parts = line[5:].strip().split("|")
                if len(parts) >= 3:
                    nodes.append({
                        "source": parts[0].strip(),
                        "relation": parts[1].strip(),
                        "target": parts[2].strip(),
                    })

        return text, nodes

    def _build_system_prompt(self, graph_context: str, god_nodes: list, repo_name: str) -> str:
        god_text = "\n".join(
            f"- {g['label']} (degree={g['degree']}, {g.get('community_name', '')})"
            for g in god_nodes[:10]
        )
        return f"""你是一个代码图谱分析助手。你正在分析仓库 "{repo_name}" 的知识图谱。

## 图谱遍历结果
以下是通过图遍历获取的相关节点和关系：
{graph_context[:4000]}

## 核心架构节点（God Nodes — 连接度最高的节点）
{god_text}

## 回答规则
1. 基于图谱中的节点和关系回答问题
2. 引用具体的节点名称和文件路径
3. 说明节点之间的关系类型（references, calls, imports 等）
4. 如果图谱中没有相关信息，明确说明
5. 使用中文回答
6. 回答要简洁、有条理"""

    async def _call_llm_stream(self, messages: list[dict]):
        """Call QuickRouter API with streaming, yield content tokens."""
        httpx = _get_httpx()
        async with httpx.AsyncClient(timeout=60.0) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": messages,
                    "stream": True,
                    "temperature": 0.7,
                },
            ) as response:
                if response.status_code != 200:
                    body = await response.aread()
                    raise Exception(f"API returned {response.status_code}: {body.decode()}")

                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str.strip() == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            yield content
                    except json.JSONDecodeError:
                        continue
