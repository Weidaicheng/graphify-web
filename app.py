"""Graphify Admin — FastAPI application entry point."""

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from database import Database
from repo_manager import RepoManager
from graph_manager import GraphManager
from chat_engine import ChatEngine

# ── paths ────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = BASE_DIR / "data"
CONFIG_PATH = BASE_DIR / "config.json"

# ── globals ──────────────────────────────────────────────────

db: Database = None
repo_mgr: RepoManager = None
graph_mgr: GraphManager = None
chat_engine: ChatEngine = None
config: dict = {}

# Pipeline queue — only one build at a time
pipeline_lock = asyncio.Lock()


def load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


# ── lifespan ─────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global db, repo_mgr, graph_mgr, chat_engine, config
    config = load_config()
    db = Database(DATA_DIR / "graphify_admin.db")
    graphify_bin = config.get("graphify_bin", "graphify")
    repo_mgr = RepoManager(db, DATA_DIR / "repos", graphify_bin)
    graph_mgr = GraphManager(db)
    chat_engine = ChatEngine(
        graph_mgr,
        config.get("llm", {}),
        graphify_bin,
    )
    print(f"✓ Graphify Admin started on http://127.0.0.1:{config.get('port', 8900)}")
    yield


app = FastAPI(title="Graphify Admin", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ═══════════════════════════════════════════════════════════
# PAGE ROUTES
# ═══════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def page_dashboard(request: Request):
    stats = db.get_dashboard_stats()
    jobs = db.list_jobs(limit=10)
    return templates.TemplateResponse(request, "dashboard.html", {
        "stats": stats, "jobs": jobs,
    })


@app.get("/repos", response_class=HTMLResponse)
async def page_repos(request: Request):
    repos = db.list_repos()
    return templates.TemplateResponse(request, "repos.html", {"repos": repos})


@app.get("/graphs", response_class=HTMLResponse)
async def page_graphs(request: Request):
    graphs = db.list_graphs()
    return templates.TemplateResponse(request, "graphs.html", {"graphs": graphs})


@app.get("/graphs/{graph_id}", response_class=HTMLResponse)
async def page_graph_viewer(request: Request, graph_id: str):
    graph = db.get_graph(graph_id)
    if not graph:
        return templates.TemplateResponse(request, "dashboard.html", {
            "stats": db.get_dashboard_stats(), "jobs": [],
            "error": "图谱不存在",
        })
    stats = graph_mgr.get_stats(graph_id)
    communities = graph_mgr.get_communities(graph_id)
    god_nodes = graph_mgr.get_god_nodes(graph_id, top_n=10)
    return templates.TemplateResponse(request, "graph_viewer.html", {
        "graph": graph,
        "stats": stats,
        "communities": communities,
        "god_nodes": god_nodes,
    })


@app.get("/chat/{graph_id}", response_class=HTMLResponse)
async def page_chat(request: Request, graph_id: str):
    graph = db.get_graph(graph_id)
    if not graph:
        return templates.TemplateResponse(request, "graphs.html", {
            "graphs": db.list_graphs(),
            "error": "图谱不存在",
        })
    graphs = db.list_graphs()
    return templates.TemplateResponse(request, "chat.html", {
        "graph": graph, "graphs": graphs,
    })


# ═══════════════════════════════════════════════════════════
# API ROUTES — REPOS
# ═══════════════════════════════════════════════════════════

@app.get("/api/repos")
async def api_list_repos():
    return db.list_repos()


@app.post("/api/repos")
async def api_add_repo(req: Request, background_tasks: BackgroundTasks):
    body = await req.json()
    github_url = body.get("github_url", "").strip()
    if not github_url:
        return JSONResponse({"error": "请提供 GitHub URL"}, 400)

    async def run_pipeline():
        async with pipeline_lock:
            await repo_mgr.clone_and_build(github_url)

    background_tasks.add_task(run_pipeline)
    return {"ok": True, "message": "正在克隆并构建图谱…"}


@app.delete("/api/repos/{repo_id}")
async def api_delete_repo(repo_id: str):
    result = await repo_mgr.delete_repo(repo_id)
    if "error" in result:
        return JSONResponse(result, 404)
    return result


@app.post("/api/repos/{repo_id}/build")
async def api_rebuild_repo(repo_id: str, background_tasks: BackgroundTasks):
    async def run():
        async with pipeline_lock:
            await repo_mgr.build_graph(repo_id)

    background_tasks.add_task(run)
    return {"ok": True, "message": "正在重新构建图谱…"}


@app.post("/api/repos/{repo_id}/update")
async def api_update_repo(repo_id: str, background_tasks: BackgroundTasks):
    async def run():
        async with pipeline_lock:
            await repo_mgr.update_graph(repo_id)

    background_tasks.add_task(run)
    return {"ok": True, "message": "正在增量更新…"}


# ═══════════════════════════════════════════════════════════
# API ROUTES — GRAPHS
# ═══════════════════════════════════════════════════════════

@app.get("/api/graphs")
async def api_list_graphs():
    return db.list_graphs()


@app.get("/api/graphs/{graph_id}")
async def api_get_graph(graph_id: str):
    graph = db.get_graph(graph_id)
    if not graph:
        return JSONResponse({"error": "图谱不存在"}, 404)
    stats = graph_mgr.get_stats(graph_id)
    return {"graph": graph, "stats": stats}


@app.delete("/api/graphs/{graph_id}")
async def api_delete_graph(graph_id: str):
    result = graph_mgr.delete_graph(graph_id)
    if "error" in result:
        return JSONResponse(result, 404)
    return result


@app.get("/api/graphs/{graph_id}/d3-data")
async def api_d3_data(
    graph_id: str,
    max_nodes: int = 500,
    community: int | None = None,
    file_type: str | None = None,
):
    data = graph_mgr.get_d3_data(graph_id, max_nodes, community, file_type)
    if not data:
        return JSONResponse({"error": "图谱数据不可用"}, 404)
    return data


@app.get("/api/graphs/{graph_id}/communities")
async def api_communities(graph_id: str):
    return graph_mgr.get_communities(graph_id)


@app.get("/api/graphs/{graph_id}/god-nodes")
async def api_god_nodes(graph_id: str, top_n: int = 10):
    return graph_mgr.get_god_nodes(graph_id, top_n)


@app.get("/api/graphs/{graph_id}/node/{node_id:path}")
async def api_node_detail(graph_id: str, node_id: str):
    detail = graph_mgr.get_node_detail(graph_id, node_id)
    if not detail:
        return JSONResponse({"error": "节点不存在"}, 404)
    return detail


# ═══════════════════════════════════════════════════════════
# API ROUTES — CHAT
# ═══════════════════════════════════════════════════════════

@app.post("/api/chat/{graph_id}")
async def api_chat(graph_id: str, req: Request):
    body = await req.json()
    query = body.get("query", "").strip()
    history = body.get("history", [])
    if not query:
        return JSONResponse({"error": "请输入问题"}, 400)

    graph = db.get_graph(graph_id)
    if not graph:
        return JSONResponse({"error": "图谱不存在"}, 404)

    async def event_stream():
        async for event in chat_engine.chat_stream(graph_id, query, history):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ═══════════════════════════════════════════════════════════
# API ROUTES — JOBS
# ═══════════════════════════════════════════════════════════

@app.get("/api/jobs")
async def api_list_jobs(repo_id: str = None):
    return db.list_jobs(repo_id=repo_id)


@app.get("/api/jobs/{job_id}")
async def api_get_job(job_id: str):
    job = db.get_job(job_id)
    if not job:
        return JSONResponse({"error": "任务不存在"}, 404)
    return job


# ── main ─────────────────────────────────────────────────────

def main():
    port = config.get("port", 8900) if config else 8900
    uvicorn.run("app:app", host="127.0.0.1", port=port, reload=True)


if __name__ == "__main__":
    main()
