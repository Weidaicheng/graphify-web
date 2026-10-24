"""Graphify Web — FastAPI application entry point."""

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from database import Database
from repo_manager import RepoManager
from graph_manager import GraphManager
from chat_engine import ChatEngine
from github_api import GitHubAPI

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

# Reserved path prefixes that must not be treated as user/repo
RESERVED_USERS = {"api", "static", "favicon.ico"}


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
    db = Database(DATA_DIR / "graphify_web.db")
    graphify_bin = config.get("graphify_bin", "graphify")
    repo_mgr = RepoManager(db, DATA_DIR / "repos", graphify_bin)
    graph_mgr = GraphManager(db)
    chat_engine = ChatEngine(
        graph_mgr,
        config.get("llm", {}),
        graphify_bin,
    )
    print(f"✓ Graphify Web started on http://127.0.0.1:{config.get('port', 8900)}")
    yield


app = FastAPI(title="Graphify Web", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _guard_reserved(user: str):
    if user in RESERVED_USERS:
        raise HTTPException(status_code=404, detail="Not found")


# ═══════════════════════════════════════════════════════════
# API ROUTES
# ═══════════════════════════════════════════════════════════

@app.get("/api/repos/{user}/{repo}/status")
async def api_repo_status(user: str, repo: str):
    """Return repo status with GitHub commit comparison."""
    _guard_reserved(user)
    record = db.get_repo(user, repo)
    if not record:
        return {"status": "no_graph", "owner": user, "name": repo}

    # Check GitHub for latest commit
    latest_sha = await GitHubAPI.get_latest_commit(user, repo)
    is_outdated = (
        latest_sha is not None
        and record.get("last_built_sha")
        and latest_sha != record["last_built_sha"]
    )

    return {
        "status": record["status"],
        "owner": user,
        "name": repo,
        "node_count": record.get("node_count", 0),
        "link_count": record.get("link_count", 0),
        "community_count": record.get("community_count", 0),
        "last_commit_sha": latest_sha,
        "last_built_sha": record.get("last_built_sha"),
        "is_outdated": is_outdated,
        "error_message": record.get("error_message"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
    }


@app.post("/api/repos/{user}/{repo}/build")
async def api_build_repo(user: str, repo: str, background_tasks: BackgroundTasks):
    _guard_reserved(user)

    async def run_pipeline():
        async with pipeline_lock:
            await repo_mgr.clone_and_build(user, repo)

    background_tasks.add_task(run_pipeline)
    return {"ok": True, "message": "正在克隆并构建图谱…"}


@app.post("/api/repos/{user}/{repo}/update")
async def api_update_repo(user: str, repo: str, background_tasks: BackgroundTasks):
    _guard_reserved(user)

    async def run_pipeline():
        async with pipeline_lock:
            await repo_mgr.update_graph(user, repo)

    background_tasks.add_task(run_pipeline)
    return {"ok": True, "message": "正在增量更新…"}


@app.delete("/api/repos/{user}/{repo}")
async def api_delete_repo(user: str, repo: str):
    _guard_reserved(user)
    result = await repo_mgr.delete_repo(user, repo)
    if "error" in result:
        return JSONResponse(result, 404)
    return result


@app.get("/api/repos/{user}/{repo}/d3-data")
async def api_d3_data(
    user: str, repo: str,
    max_nodes: int = 500,
    community: int | None = None,
    file_type: str | None = None,
):
    _guard_reserved(user)
    data = graph_mgr.get_d3_data(user, repo, max_nodes, community, file_type)
    if not data:
        return JSONResponse({"error": "图谱数据不可用"}, 404)
    return data


@app.get("/api/repos/{user}/{repo}/communities")
async def api_communities(user: str, repo: str):
    _guard_reserved(user)
    return graph_mgr.get_communities(user, repo)


@app.get("/api/repos/{user}/{repo}/god-nodes")
async def api_god_nodes(user: str, repo: str, top_n: int = 10):
    _guard_reserved(user)
    return graph_mgr.get_god_nodes(user, repo, top_n)


@app.get("/api/repos/{user}/{repo}/node/{node_id:path}")
async def api_node_detail(user: str, repo: str, node_id: str):
    _guard_reserved(user)
    detail = graph_mgr.get_node_detail(user, repo, node_id)
    if not detail:
        return JSONResponse({"error": "节点不存在"}, 404)
    return detail


@app.post("/api/repos/{user}/{repo}/chat")
async def api_chat(user: str, repo: str, req: Request):
    _guard_reserved(user)
    body = await req.json()
    query = body.get("query", "").strip()
    history = body.get("history", [])
    if not query:
        return JSONResponse({"error": "请输入问题"}, 400)

    record = db.get_repo(user, repo)
    if not record or not record.get("graph_json_path"):
        return JSONResponse({"error": "图谱尚未构建"}, 404)

    async def event_stream():
        async for event in chat_engine.chat_stream(user, repo, query, history):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/api/jobs/{user}/{repo}")
async def api_latest_job(user: str, repo: str):
    _guard_reserved(user)
    job = db.get_latest_job(user, repo)
    if not job:
        return JSONResponse({"error": "没有构建任务"}, 404)
    return job


# ═══════════════════════════════════════════════════════════
# PAGE ROUTES (catch-all last)
# ═══════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def page_dashboard(request: Request):
    repos = db.list_repos()
    return templates.TemplateResponse(request, "dashboard.html", {
        "repos": repos,
    })


@app.get("/{user}/{repo}/graph", response_class=HTMLResponse)
async def page_graph_viewer(user: str, repo: str, request: Request):
    _guard_reserved(user)
    record = db.get_repo(user, repo)
    if not record or not record.get("graph_json_path"):
        raise HTTPException(status_code=404, detail="图谱不存在")

    stats = graph_mgr.get_stats(user, repo)
    communities = graph_mgr.get_communities(user, repo)
    god_nodes = graph_mgr.get_god_nodes(user, repo, top_n=10)
    return templates.TemplateResponse(request, "graph_viewer.html", {
        "owner": user,
        "name": repo,
        "repo": record,
        "stats": stats,
        "communities": communities,
        "god_nodes": god_nodes,
    })


@app.get("/{user}/{repo}/chat", response_class=HTMLResponse)
async def page_chat(user: str, repo: str, request: Request):
    _guard_reserved(user)
    record = db.get_repo(user, repo)
    if not record or not record.get("graph_json_path"):
        raise HTTPException(status_code=404, detail="图谱尚未构建")

    return templates.TemplateResponse(request, "chat.html", {
        "owner": user,
        "name": repo,
        "repo": record,
    })


@app.get("/{user}/{repo}", response_class=HTMLResponse)
async def page_repo_status(user: str, repo: str, request: Request):
    _guard_reserved(user)
    record = db.get_repo(user, repo)
    # Check GitHub for latest commit
    latest_sha = await GitHubAPI.get_latest_commit(user, repo)
    is_outdated = (
        latest_sha is not None
        and record
        and record.get("last_built_sha")
        and latest_sha != record["last_built_sha"]
    )
    return templates.TemplateResponse(request, "repo_status.html", {
        "owner": user,
        "name": repo,
        "repo": record,
        "latest_sha": latest_sha,
        "is_outdated": is_outdated,
    })


# ── main ─────────────────────────────────────────────────────

def main():
    port = config.get("port", 8900) if config else 8900
    uvicorn.run("app:app", host="127.0.0.1", port=port, reload=True)


if __name__ == "__main__":
    main()
