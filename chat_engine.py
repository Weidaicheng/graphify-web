"""Chat engine — graph-context RAG with LLM streaming + query expansion."""

import asyncio
import json
import os
import re

from graph_manager import GraphManager

# Lazy import httpx to avoid startup errors if not installed
_httpx = None


def _get_httpx():
    global _httpx
    if _httpx is None:
        import httpx
        _httpx = httpx
    return _httpx


def _get_proxy_kwargs() -> dict:
    """Build httpx client kwargs with explicit proxy to avoid NO_PROXY IPv6 bug."""
    proxy_url = (
        os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY")
    )
    kwargs = {"timeout": 60.0, "trust_env": False}
    if proxy_url:
        kwargs["proxy"] = proxy_url
    return kwargs


class ChatEngine:
    def __init__(self, graph_manager: GraphManager, llm_config: dict, graphify_bin: str):
        self.graph_mgr = graph_manager
        self.base_url = llm_config.get("base_url", "https://api.quickrouter.ai/v1")
        self.api_key = llm_config.get("api_key", "")
        self.model = llm_config.get("model", "gpt-4o-mini")
        self.model_fast = llm_config.get("model_fast", self.model)
        self.graphify_bin = graphify_bin
        # Vocabulary cache: graph_json_path → sorted token list
        self._vocab_cache: dict[str, list[str]] = {}

    # ── Step 0: Query Expansion ────────────────────────────────

    def _extract_vocab(self, graph_json_path: str) -> list[str]:
        """Extract token vocabulary from node labels in graph.json.

        Splits on camelCase, lowercases, filters to 3-30 char tokens.
        Results are cached per graph path.
        """
        if graph_json_path in self._vocab_cache:
            return self._vocab_cache[graph_json_path]

        try:
            with open(graph_json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            self._vocab_cache[graph_json_path] = []
            return []

        vocab = set()
        for node in data.get("nodes", []):
            label = node.get("label", "") or ""
            # Split into words on non-alphanumeric boundaries
            for word in re.findall(r"[^\W\d_]+", label, re.UNICODE):
                # Split camelCase: "LoginPage" → ["Login", "Page"]
                parts = re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+", word) or [word]
                for part in parts:
                    token = parts[0].lower() if parts else word.lower()
                    token = part.lower()
                    if 3 <= len(token) <= 30:
                        vocab.add(token)

        result = sorted(vocab)
        self._vocab_cache[graph_json_path] = result
        return result

    async def _expand_query(self, question: str, vocab: list[str]) -> list[str]:
        """Use LLM to select up to 12 tokens from vocab that match the question.

        Returns the selected token list (may be empty if no match).
        """
        if not vocab or not self.api_key:
            return []

        vocab_text = "\n".join(vocab)
        prompt = f"""You are a query expansion tool for a code knowledge graph.

The user asked: "{question}"

Below is the complete vocabulary extracted from the graph's node labels.
Select UP TO 12 tokens from this EXACT vocabulary that semantically match the user's query intent.

RULES:
- You MUST pick ONLY tokens present in the vocabulary list below. Do NOT invent tokens.
- If a concept has no plausible token in the vocab, skip it.
- Translate cross-language: if the user asks in Chinese, find the English equivalent tokens.
- Handle morphology: "handlers" → "handler" if present, "登录" → "login" if present.
- If NO tokens match at all, respond with an empty list.

Respond with ONLY a JSON array of strings, e.g.: ["login", "auth", "gateway"]
Do NOT include any explanation or markdown formatting.

Vocabulary:
{vocab_text}"""

        try:
            result = await self._call_llm_single(prompt)
            # Extract JSON array from response
            result = result.strip()
            # Handle markdown code blocks
            if "```" in result:
                result = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", result, re.DOTALL)
                if result:
                    result = result.group(1)
            # Find the JSON array
            match = re.search(r"\[.*?\]", result, re.DOTALL)
            if match:
                tokens = json.loads(match.group())
                # Validate: only keep tokens actually in vocab
                vocab_set = set(vocab)
                return [t for t in tokens if isinstance(t, str) and t.lower() in vocab_set][:12]
        except Exception as e:
            print(f"[query expansion] LLM error: {e}")

        return []

    def _detect_traversal_mode(self, question: str) -> str:
        """Detect BFS vs DFS based on query intent keywords."""
        dfs_keywords = ["链路", "路径", "调用链", "流程", "path", "chain", "how does",
                        "trace", "reach", "从", "到", "怎么", "如何"]
        q_lower = question.lower()
        for kw in dfs_keywords:
            if kw in q_lower:
                return "dfs"
        return "bfs"

    def _should_suggest_diagram(self, question: str) -> bool:
        """Detect if the query is likely to benefit from a Mermaid flow diagram."""
        diagram_keywords = [
            "架构", "流程", "调用链", "链路", "依赖", "数据流",
            "怎么工作", "如何工作", "怎么实现", "如何调用",
            "architecture", "flow", "pipeline", "chain", "how does",
            "模块关系", "组件关系", "拓扑", "结构",
            "入口", "出口", "路由", "转发", "调用",
        ]
        q_lower = question.lower()
        return any(kw in q_lower for kw in diagram_keywords)

    # ── Main chat flow ─────────────────────────────────────────

    async def chat_stream(self, owner: str, name: str, query: str, history: list[dict] = None):
        """
        Generator yielding SSE event dicts:
        {"type": "expansion", "tokens": [...], "mode": "bfs"|"dfs"} → query expansion result
        {"type": "context", ...} → graph context used
        {"type": "token", "content": "..."} → LLM token
        {"type": "done"} → stream complete
        {"type": "error", "message": "..."} → error
        """
        repo = self.graph_mgr.db.get_repo(owner, name)
        if not repo:
            yield {"type": "error", "message": "仓库不存在"}
            return

        graph_json_path = repo.get("graph_json_path")
        if not graph_json_path:
            yield {"type": "error", "message": "图谱尚未构建"}
            return

        repo_name = f"{owner}/{name}"

        # Step 0: Query expansion
        vocab = self._extract_vocab(graph_json_path)
        expanded_tokens = []
        mode = self._detect_traversal_mode(query)

        if self.api_key and vocab:
            expanded_tokens = await self._expand_query(query, vocab)

        # Build the actual query string from expanded tokens, fallback to original
        if expanded_tokens:
            expanded_query = " ".join(expanded_tokens)
        else:
            expanded_query = query

        # Yield expansion event for frontend auditability
        yield {
            "type": "expansion",
            "original": query,
            "tokens": expanded_tokens,
            "mode": mode,
            "vocab_size": len(vocab),
        }

        # Step 1: Graph traversal with expanded query
        context_text, context_nodes = await self._query_graph(
            graph_json_path, expanded_query, mode=mode,
        )

        # Get god nodes for architectural context
        god_nodes = self.graph_mgr.get_god_nodes(owner, name, top_n=10)

        # Yield context event
        yield {
            "type": "context",
            "nodes": context_nodes[:20],
            "god_nodes": [g["label"] for g in god_nodes[:5]],
        }

        # Step 2: Build system prompt with enhanced rules
        system_prompt = self._build_system_prompt(
            context_text, god_nodes, repo_name, expanded_tokens,
        )

        # Step 3: Build messages
        messages = [{"role": "system", "content": system_prompt}]
        if history:
            for msg in history[-10:]:
                messages.append({"role": msg["role"], "content": msg["content"]})

        # Add diagram hint for architecture/flow queries
        user_content = query
        if self._should_suggest_diagram(query):
            user_content += "\n\n[提示：此问题适合用 Mermaid 流程图回答。如果图谱中有足够的关系信息，请生成 ```mermaid 代码块来展示流程或架构]"
        messages.append({"role": "user", "content": user_content})

        # Step 4: Call LLM with streaming
        if not self.api_key:
            yield {"type": "token", "content": f"⚠️ 未配置 LLM API key，以下是图谱查询结果：\n\n{context_text}"}
            yield {"type": "done"}
            return

        try:
            async for token in self._call_llm_stream(messages):
                yield {"type": "token", "content": token}
            yield {"type": "done"}
        except Exception as e:
            yield {"type": "error", "message": f"LLM 调用失败: {str(e)}"}

    # ── Graph traversal ────────────────────────────────────────

    async def _query_graph(
        self, graph_json_path: str, question: str, mode: str = "bfs",
    ) -> tuple[str, list[dict]]:
        """Run graphify query and parse output. Returns (raw_text, parsed_nodes)."""
        cmd = [
            self.graphify_bin, "query", question,
            "--graph", graph_json_path,
            "--budget", "3000",
        ]
        if mode == "dfs":
            cmd.append("--dfs")

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
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

    # ── Prompt construction ────────────────────────────────────

    def _build_system_prompt(
        self, graph_context: str, god_nodes: list,
        repo_name: str, expanded_tokens: list[str] = None,
    ) -> str:
        god_text = "\n".join(
            f"- {g['label']} (degree={g['degree']}, {g.get('community_name', '')})"
            for g in god_nodes[:10]
        )
        expansion_note = ""
        if expanded_tokens:
            expansion_note = f"\n\n## 查询扩展\n原始问题通过词汇匹配扩展为: {', '.join(expanded_tokens)}\n请结合扩展后的语义进行回答。"

        return f"""你是一个代码图谱分析助手。你正在分析仓库 "{repo_name}" 的知识图谱。

## 图谱遍历结果
以下是通过图遍历获取的相关节点和关系：
{graph_context[:4000]}

## 核心架构节点（God Nodes — 连接度最高的节点）
{god_text}
{expansion_note}

## 回答规则
1. 只基于图谱中的节点和关系回答，不要编造图谱中没有的信息
2. 引用具体的节点名称和文件路径（source_file）
3. 引用代码位置时使用 source_location（如 L42）
4. 说明节点之间的关系类型（references, calls, imports, contains 等）
5. 区分确定性：EXTRACTED（源码直接提取）> INFERRED（推断）> AMBIGUOUS（不确定）
6. 如果图谱中没有相关信息，明确说明
7. 使用中文回答
8. 回答要有条理，使用列表和结构化格式
9. 当问题涉及架构流程、调用链路、模块依赖、数据流向时，使用 Mermaid 语法生成流程图。格式：用 ```mermaid 代码块包裹，使用 flowchart TD 或 graph LR。节点 ID 使用英文字母，节点标签必须用双引号包裹（因为标签中可能包含冒号、括号等特殊字符）。示例：
```mermaid
flowchart TD
    A["用户请求"] --> B["网关鉴权"]
    B --> C{"认证通过?"}
    C -->|"是"| D["转发上游服务"]
    C -->|"否"| E["返回 401"]
```
10. 流程图应基于图谱中的实际节点和关系构建，不要编造图谱中不存在的连接
11. 对于复杂架构，可以同时提供流程图和文字说明。先给流程图，再给详细解释
12. 当问题不适合用图表回答时（如简单事实查询），不需要强制生成图表"""

    # ── LLM calls ──────────────────────────────────────────────

    async def _call_llm_single(self, prompt: str) -> str:
        """Single (non-streaming) LLM call, returns full response text."""
        httpx = _get_httpx()
        client_kwargs = _get_proxy_kwargs()
        client_kwargs["timeout"] = 30.0

        async with httpx.AsyncClient(**client_kwargs) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model_fast,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "temperature": 0.0,
                    "max_tokens": 200,
                },
            )
            if resp.status_code != 200:
                raise Exception(f"API returned {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
            return data["choices"][0]["message"]["content"]

    async def _call_llm_stream(self, messages: list[dict]):
        """Call QuickRouter API with streaming, yield content tokens."""
        httpx = _get_httpx()
        client_kwargs = _get_proxy_kwargs()

        async with httpx.AsyncClient(**client_kwargs) as client:
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
                        choices = chunk.get("choices", [])
                        if not choices:
                            continue
                        delta = choices[0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            yield content
                    except json.JSONDecodeError:
                        continue
