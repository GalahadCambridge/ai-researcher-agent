"""FastAPI 应用（详细设计 1 第 8.1、9 节）。air serve 启动它；只监听 127.0.0.1，本期单用户不做登录。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qsl

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import airesearcher
from airesearcher.core.errors import AirError
from airesearcher.core.i18n import Msg, pick_lang, render, set_lang
from airesearcher.core.workspace import repo_root

from . import sse
from .manager import ProjectManager
from .routes import approvals, artifacts, export, models, paper, projects, runs, templates


def _error(code: str, message: str, status: int, detail: dict | None = None, msg: Msg | None = None) -> JSONResponse:
    body = {"code": code, "message": render(msg) or message, "detail": detail or {}}
    if msg is not None:  # 界面想自己翻译时用 message_code + params
        body |= {"message_code": msg.code, "params": msg.params}
    return JSONResponse({"error": body}, status_code=status)


class LanguageMiddleware:
    """每个请求开始时确定界面语言：?lang=en|zh 优先，其次 Accept-Language，默认 zh（详细设计 1 第 9.6 节）。

    写成纯 ASGI 中间件：语言存在 contextvar 里，路由（含线程池里的同步路由）、错误处理、SSE 都能读到。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            query = dict(parse_qsl(scope.get("query_string", b"").decode("latin-1")))
            headers = dict(scope.get("headers") or [])
            set_lang(pick_lang(query.get("lang"), headers.get(b"accept-language", b"").decode("latin-1")))
        await self.app(scope, receive, send)


def create_app(home: Path | None = None, start_engines: bool = True, engine_kwargs: dict | None = None) -> FastAPI:
    mgr = ProjectManager(home=home, start_engines=start_engines, engine_kwargs=engine_kwargs)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        mgr.startup()  # 启动核对（5.6），并为每个项目启动引擎线程
        yield
        mgr.shutdown()

    app = FastAPI(title="AI Researcher Agent API", version=airesearcher.__version__, lifespan=lifespan,
                  description="主干后台 API。CLI 与 GUI 都是它的客户端（D4）。错误格式见详细设计 1 第 9.1 节。")
    app.state.manager = mgr
    app.add_middleware(LanguageMiddleware)

    @app.exception_handler(AirError)
    async def air_error(_: Request, e: AirError) -> JSONResponse:
        return _error(e.code, e.message, e.http_status, e.detail, e.msg)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, e: RequestValidationError) -> JSONResponse:
        return _error("VALIDATION", "", 422, {"errors": e.errors()}, Msg(code="error.invalid_params"))

    @app.exception_handler(FileNotFoundError)
    async def not_found(_: Request, e: FileNotFoundError) -> JSONResponse:
        return _error("NOT_FOUND", str(e), 404)

    @app.get("/api/health", tags=["meta"])
    def health() -> dict:
        return {"ok": True, "version": airesearcher.__version__, "projects": len(mgr.projects)}

    for r in (projects.router, approvals.router, artifacts.router, runs.router, paper.router, models.router,
              templates.router, export.router, sse.router):
        app.include_router(r)

    dist = repo_root() / "web" / "dist"
    if dist.exists():  # 前端交付后由同一服务提供（8.1）
        app.mount("/", StaticFiles(directory=dist, html=True), name="web")
    return app
