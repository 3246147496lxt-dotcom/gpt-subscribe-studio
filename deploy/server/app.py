"""自有服务器入口：同源提供公私隔离 API 与双入口 SPA 前端（/ 用户端、/admin 管理工作台）。"""

import os
from pathlib import Path

from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from gss_api.main import create_app
from gss_api.test_access import TestAccessMiddleware

app = create_app()
app.add_middleware(
    TestAccessMiddleware,
    credentials_file=os.environ["GSS_AUTH_FILE"],
    login_file=os.environ.get("GSS_LOGIN_FILE", "/app/login.html"),
    origin=os.environ.get("GSS_PUBLIC_ORIGIN", ""),
)

_web_dir = Path(os.environ.get("GSS_WEB_DIR", "/app/web"))
_index_html = _web_dir / "index.html"


@app.get("/admin", include_in_schema=False)
@app.get("/admin/{subpath:path}", include_in_schema=False)
async def serve_admin_spa(subpath: str = ""):
    del subpath
    return FileResponse(_index_html, headers={"Cache-Control": "no-store"})


app.mount("/", StaticFiles(directory=str(_web_dir), html=True), name="web")
