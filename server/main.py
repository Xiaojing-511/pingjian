"""评见 API。

浏览器只谈 REST 和 SSE。DeepSeek 的分类调用在工具内部，DSH 只编排这六个工具。
同一进程同时只跑一个分析。密钥缺失时进程仍能启动，健康检查会写明缺哪一项。
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from server.api import register
from server.auth import CloudBaseAuth
from server.cloudbase import CloudBaseStore
from server.config import load_settings
from server.deepseek import DeepSeekClient
from server.dsh_runner import dsh_status
from server.prompts import ORCHESTRATOR_SYSTEM
from server.queue import AnalysisQueue
from server.store import UnavailableStore


class UTF8JSONResponse(JSONResponse):
    def render(self, content) -> bytes:
        return json.dumps(content, ensure_ascii=False).encode("utf-8")


def create_app(
    settings=None,
    store=None,
    auth=None,
    model=None,
    orchestrator: str = "dsh",
    inline: bool = False,
) -> FastAPI:
    settings = settings or load_settings()
    # 插件和 DSH 子进程都从环境变量读令牌与编排提示，不把令牌写进镜像。
    os.environ["PINGJIAN_TOOL_TOKEN"] = settings.tool_token
    os.environ["PINGJIAN_INTERNAL_BASE"] = settings.internal_base
    os.environ["DSH_SYSTEM_PROMPT"] = ORCHESTRATOR_SYSTEM
    os.environ["DSH_HOME"] = settings.dsh_home
    if store is None:
        if settings.cloudbase_api_key:
            store = CloudBaseStore(settings.cloudbase_env_id, settings.cloudbase_api_key)
        else:
            store = UnavailableStore(["CLOUDBASE_API_KEY"])
    if auth is None:
        auth = CloudBaseAuth(settings.cloudbase_env_id)
    if model is None:
        model = DeepSeekClient(settings.deepseek_api_key, settings.deepseek_model)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.startup_error = None
        if not app.state.inline:
            app.state.queue.start()
            if isinstance(app.state.store, CloudBaseStore):
                try:
                    await asyncio.to_thread(app.state.store.ensure_collections)
                    await asyncio.to_thread(app.state.queue.recover)
                except Exception as exc:
                    app.state.startup_error = str(exc)[:300]
        yield
        app.state.queue.stop()

    app = FastAPI(title="评见", default_response_class=UTF8JSONResponse, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["*"],
    )
    app.state.settings = settings
    app.state.store = store
    app.state.auth = auth
    app.state.model = model
    app.state.inline = inline
    app.state.user_cache = {}
    app.state.startup_error = None
    app.state.dsh_status = dsh_status
    app.state.queue = AnalysisQueue(store, settings, model, orchestrator, inline)
    register(app)
    return app


app = create_app()
