from __future__ import annotations

import asyncio
import re
import shutil
import threading
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config
from .auth import SessionManager
from .crawler import CrawlOptions, Crawler
from .db import UNKNOWN_DEPTH, GraphDB
from .ig_client import DEFAULT_UA, AuthError, IGError, NotAllowed

STATIC = Path(__file__).parent / "static"

db = GraphDB(config.DB_PATH)
sessions = SessionManager()
crawler = Crawler(db, sessions)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    asyncio.create_task(sessions.init())
    yield
    await crawler.stop()
    await sessions.close()


app = FastAPI(title="Instagraph", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")

_USERNAME = re.compile(r"^[A-Za-z0-9._]{1,30}$")
_RESERVED = {"accounts", "p", "reel", "reels", "explore", "stories", "direct", "tv"}


def parse_username(text: str) -> str:
    text = text.strip()
    m = re.search(r"instagram\.com/([^/?#\s]+)", text)
    name = m.group(1) if m else text.lstrip("@").strip("/")
    if name.lower() in _RESERVED or not _USERNAME.match(name):
        raise HTTPException(400, "Paste a profile link like https://www.instagram.com/yourname/ or a @username")
    return name.lower()


class StartRequest(BaseModel):
    url: str
    depth: int = Field(1, ge=1, le=5)
    max_accounts: int = Field(200, ge=1, le=50000)
    max_per_list: int = Field(1000, ge=12, le=200000)
    concurrency: int = Field(4, ge=1, le=32)
    delay_ms: int = Field(300, ge=0, le=60000)
    include_followers: bool = True
    include_following: bool = True
    skip_verified: bool = True
    retry_errors: bool = True


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/status")
async def status():
    return {"session": sessions.info(), "crawl": await asyncio.to_thread(crawler.status)}


@app.post("/api/login")
async def login():
    sessions.start_login()
    return {"ok": True}


@app.post("/api/logout")
async def logout():
    if crawler.running:
        raise HTTPException(409, "Stop the crawl first")
    await sessions.logout()
    return {"ok": True}


@app.post("/api/crawl/start")
async def start(req: StartRequest):
    if crawler.running:
        raise HTTPException(409, "A crawl is already running")
    if not (req.include_followers or req.include_following):
        raise HTTPException(400, "Select followers and/or following")
    seed = parse_username(req.url)
    if req.retry_errors:
        db.retry_errors()
    opts = CrawlOptions(**req.model_dump(exclude={"url", "retry_errors"}))
    await crawler.start(seed, opts)
    return {"ok": True, "seed": seed}


@app.post("/api/crawl/stop")
async def stop():
    await crawler.stop()
    return {"ok": True}


@app.post("/api/reset")
async def reset():
    if crawler.running:
        raise HTTPException(409, "Stop the crawl first")
    await asyncio.to_thread(db.clear)
    shutil.rmtree(config.AVATAR_DIR, ignore_errors=True)
    config.AVATAR_DIR.mkdir(exist_ok=True)
    crawler.seed = None
    crawler.log.clear()
    return {"ok": True}


@app.get("/api/graph")
async def graph(min_degree: int = 2, max_nodes: int = 2000, focus: str = "", extra: str = ""):
    min_degree = max(1, min_degree)
    max_nodes = max(10, min(max_nodes, 50000))
    focus_pk = focus if focus.isdigit() else None
    extra_pks = [x for x in extra.split(",") if x.isdigit()][:200]
    return await asyncio.to_thread(db.graph, min_degree, max_nodes, focus_pk, extra_pks)


class ExpandRequest(BaseModel):
    pk: str


@app.post("/api/expand")
async def expand(req: ExpandRequest):
    if not req.pk.isdigit() or not db.get_user(req.pk):
        raise HTTPException(404, "Unknown user")
    try:
        queued = await crawler.expand(req.pk)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"ok": True, "queued": queued}


class PathRequest(BaseModel):
    username: str


@app.post("/api/path")
async def find_path(req: PathRequest):
    name = parse_username(req.username)
    seed_pk = db.get_meta("seed")
    if not seed_pk:
        raise HTTPException(400, "Crawl your own account first")
    row = db.find_username(name)
    path = await asyncio.to_thread(db.shortest_path, seed_pk, row["pk"]) if row else None
    looked_up = False
    if path is None and (row is None or row["status"] not in ("done", "partial")):
        # Not connected yet: fetch their lists and look for overlap with what we know.
        try:
            client = await crawler.client()
            if row is None:
                prof = await client.profile(name)
                db.upsert_users([prof], UNKNOWN_DEPTH)
                row = db.get_user(prof["pk"])
            await crawler.crawl_single(row["pk"])
            looked_up = True
        except NotAllowed as e:
            return {"found": False, "target_pk": row["pk"] if row else None,
                    "message": f"Couldn't read @{name}'s lists ({e}). If they're private, you need to follow them."}
        except AuthError as e:
            raise HTTPException(401, str(e))
        except IGError as e:
            raise HTTPException(502, f"Instagram error: {e}")
        path = await asyncio.to_thread(db.shortest_path, seed_pk, row["pk"])
    if path is None:
        total = db.counts()["users"]
        return {"found": False, "target_pk": row["pk"] if row else None,
                "message": f"No connection between you and @{name} among the {total:,} people found so far. "
                           "Expand people you think might know them, then try again."}
    users = db.users_by_pk([s["pk"] for s in path])
    for i, s in enumerate(path):
        db.set_depth_min(s["pk"], i)
    return {
        "found": True,
        "looked_up": looked_up,
        "hops": len(path) - 1,
        "target_pk": path[-1]["pk"],
        "path": [{"pk": s["pk"], "rel": s["rel"], "username": users[s["pk"]]["username"],
                  "full_name": users[s["pk"]]["full_name"]} for s in path],
    }


@app.get("/api/export")
async def export():
    data = await asyncio.to_thread(db.export)
    return JSONResponse(data, headers={"Content-Disposition": 'attachment; filename="instagraph.json"'})


_avatar_http: httpx.AsyncClient | None = None


@app.get("/api/avatar/{pk}")
async def avatar(pk: str):
    global _avatar_http
    if not pk.isdigit():
        raise HTTPException(400)
    path = config.AVATAR_DIR / f"{pk}.jpg"
    if path.exists():
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})
    row = db.get_user(pk)
    if not row or not row["pic"]:
        raise HTTPException(404)
    if _avatar_http is None:
        _avatar_http = httpx.AsyncClient(timeout=15, follow_redirects=True, headers={"User-Agent": DEFAULT_UA})
    try:
        r = await _avatar_http.get(row["pic"])
        r.raise_for_status()
    except httpx.HTTPError:
        raise HTTPException(404)
    path.write_bytes(r.content)
    return Response(r.content, media_type=r.headers.get("content-type", "image/jpeg"))


def main() -> None:
    import uvicorn

    url = f"http://{config.HOST}:{config.PORT}"
    print(f"\n  Instagraph running at {url}\n")
    if config.OPEN_BROWSER:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="warning")


if __name__ == "__main__":
    main()
