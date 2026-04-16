"""
Web server -- lightweight FastAPI app that dispatches pipeline stages
and serves a UI for inspecting each step.

Run:  python -m uvicorn src.web.server:app --reload --port 8000
  or: python -m src.web.server
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from src.web.compat import LegacyRouteRewriter
from src.web.routes import api_router


def _load_env():
    root = Path(__file__).resolve().parents[2]
    for name in (".env", ".env.local"):
        p = root / name
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if k and k not in os.environ:
                        os.environ[k] = v

_load_env()

import logging
logging.basicConfig(
    level=logging.DEBUG,
    format="%(levelname)-5s %(name)s: %(message)s",
)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("anthropic").setLevel(logging.WARNING)

app = FastAPI(title="ManufacturerAI")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Rewrite legacy /api/session/…?session=X → /api/sessions/X/…
app.add_middleware(LegacyRouteRewriter)

app.include_router(api_router)

# ── Static file serving (built-in UI) ─────────────────────────────

STATIC_DIR = Path(__file__).resolve().parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.middleware("http")
    async def _no_cache_static(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response

    @app.get("/", response_class=HTMLResponse)
    async def index():
        """Serve the main HTML page."""
        html_path = STATIC_DIR / "index.html"
        if not html_path.exists():
            return HTMLResponse("<h1>ManufacturerAI</h1><p>Static files not found.</p>")
        return FileResponse(html_path)


if __name__ == "__main__":
    import uvicorn
    print("Starting ManufacturerAI server on http://localhost:8000")
    uvicorn.run("src.web.server:app", host="127.0.0.1", port=8000, reload=True)

