"""FastAPI 应用入口。默认只监听回环地址。"""

from __future__ import annotations

from contextlib import asynccontextmanager
import logging
import os
from urllib.parse import unquote, urlsplit

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from gss_core import install_logging_filter
from gss_core.config import load_settings, missing_upstreams, use_fake_adapters
from gss_core.proxies import GatewayStore, configured_proxy

from .persistence import init_db
from .store import store
from .routers import (
    account, events, locks, orders, promo, proxies, public_portal, quick_pay, saved_cards,
    protocol_cards, runtime_flags as runtime_flags_router,
    system as system_router, webhooks as webhooks_router,
)

__version__ = "0.1.0"


def _ensure_info_logging() -> None:
    gss = logging.getLogger("gss")
    gss.setLevel(logging.INFO)
    if not gss.handlers:
        handler = logging.StreamHandler()
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        gss.addHandler(handler)
    gss.propagate = False


def _env_on(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _check_pinned_proxy_sid() -> bool:
    """Warn loudly when order traffic is not pinned to one proxy SID."""
    sid = os.environ.get("GSS_PROXY_SID", "").strip()
    if not sid:
        logging.getLogger("gss").warning(
            "GSS_PROXY_SID is not set; checkout_proxy_for may generate a random SID"
        )
        if _env_on("GSS_REQUIRE_PINNED_PROXY"):
            raise RuntimeError("GSS_PROXY_SID is required for pinned order traffic")
        return False
    proxy = configured_proxy("checkout") or ""
    username = unquote(urlsplit(proxy).username or "") if proxy else ""
    if proxy and f"sid-{sid}" not in username:
        logging.getLogger("gss").warning(
            "GSS_PROXY_SID does not match the configured checkout proxy SID"
        )
    return True


def create_app() -> FastAPI:
    install_logging_filter()
    _ensure_info_logging()
    install_logging_filter(logging.getLogger("gss"))
    settings = load_settings()
    proxy_sid_pinned = _check_pinned_proxy_sid()
    db = init_db()
    store.attach_db(db)
    from .locks import recover_incomplete_jobs
    from .services import order_flow, reconciler, webhooks as webhook_svc
    recover_incomplete_jobs()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        order_flow.reset_shutdown()
        reconciler.start()
        try:
            yield
        finally:
            await reconciler.stop()
            await order_flow.drain_quick_pay(keep_draining=False)
            await webhook_svc.drain_webhooks()

    app = FastAPI(
        title="gpt-subscribe-studio API",
        version=__version__,
        description="独立的 ChatGPT 订阅开通工具：粘贴 session，浏览器填卡。",
        lifespan=lifespan,
    )

    @app.exception_handler(RequestValidationError)
    async def _private_order_input(request: Request, exc: RequestValidationError):
        # Malformed financial inputs may contain a whole address, card or session.
        path = request.url.path.rstrip("/")
        private_create = (
            path == "/api/orders"
            or path.startswith("/api/orders/")
            or path in {
                "/api/payments/quick-pay",
                "/api/payments/preflight",
                "/api/payments/preflight/batch",
                "/api/public/quick-pay",
                "/api/public/account/subscription-detail",
                "/api/public/account/billing-history",
                "/api/public/account/stripe-portal",
                "/api/public/account/cancel-auto-renew",
                "/api/public/auth/login",
                "/api/public/auth/register",
            }
            or (path.startswith("/api/public/tasks/") and path.endswith("/cancel-auto-renew"))
        )
        if request.method == "POST" and private_create:
            return JSONResponse(status_code=422, content={"detail": {
                "code": "INVALID_ORDER_INPUT", "message": "订单请求字段无效，未提交。"}})
        return await request_validation_exception_handler(request, exc)

    app.include_router(orders.router)
    app.include_router(quick_pay.router)
    app.include_router(locks.router)
    app.include_router(saved_cards.router)
    app.include_router(protocol_cards.router)
    app.include_router(promo.router)
    app.include_router(events.router)
    app.state.gateway_store = GatewayStore()
    app.include_router(account.router)
    app.include_router(proxies.router)
    app.include_router(runtime_flags_router.router)
    app.include_router(webhooks_router.router)
    app.include_router(system_router.router)
    app.include_router(public_portal.router)
    app.include_router(public_portal.admin_router)

    @app.get("/api/health", tags=["health"])
    async def health() -> dict:
        return {
            "status": "ok",
            "version": __version__,
            "promo_enabled": settings.enable_promo_mode,
            "fake_adapters": use_fake_adapters(),
            "missing_upstreams": missing_upstreams(settings),
            "proxy_sid_pinned": proxy_sid_pinned,
            "queue": order_flow.queue_stats(),
            "reconciler": reconciler.status(),
            "webhooks": webhook_svc.manager.health_summary(),
        }

    # ── 出口代理连接失败 → 503（而不是裸 500）────────────────────
    # 住宅动态代理偶发 TLS 中断：BoringSSL SSL_connect: Connection closed abruptly。
    try:
        from curl_cffi.requests.exceptions import RequestException as _CurlErr
    except Exception:  # noqa: BLE001
        _CurlErr = None  # type: ignore[assignment]
    try:
        import httpx as _httpx
        _HttpxErr = _httpx.HTTPError
    except Exception:  # noqa: BLE001
        _HttpxErr = None  # type: ignore[assignment]

    _CONN_HINTS = (
        "SSL", "Connection closed", "ConnectError", "ConnectionError",
        "CurlError", "Temporary failure", "timed out", "Timeout",
        "RemoteDisconnected", "EOF", "reset by peer",
    )

    @app.exception_handler(Exception)
    async def _conn_error_handler(request, exc):  # noqa: ANN001
        is_conn = (
            (_CurlErr is not None and isinstance(exc, _CurlErr))
            or (_HttpxErr is not None and isinstance(exc, _HttpxErr))
            or any(k in str(exc) for k in _CONN_HINTS)
        )
        if is_conn:
            return JSONResponse(
                status_code=503,
                content={"detail": "出口代理连接 chatgpt.com 失败（住宅代理断连），"
                                   "请求结果未确认，请先核对订单状态，不要重复提交。"},
            )
        raise exc

    return app


app = create_app()
