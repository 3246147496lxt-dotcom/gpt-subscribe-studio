"""SSE (Server-Sent Events) 实时订单状态推送。"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Any, AsyncGenerator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from gss_contracts.models import OrderStatus
from ..store import store

router = APIRouter(prefix="/api/events", tags=["events"])
logger = logging.getLogger("gss.api.events")

# 全局订阅管理器：order_id -> set of asyncio.Queue
_subscribers: dict[str, set[asyncio.Queue[dict]]] = {}
_lock = asyncio.Lock()

# Thread-safe, loop-aware QuickPay SSE subscribers: job_id -> set[(loop, queue)]
_qp_lock = threading.Lock()
_qp_subscribers: dict[str, set[tuple[asyncio.AbstractEventLoop, asyncio.Queue[dict]]]] = {}
_QP_FINAL_STATUSES = {"succeeded", "failed", "unknown", "paid_pending_delivery"}


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def notify_order_update(order_id: str, status: OrderStatus, data: dict | None = None) -> None:
    """通知所有订阅该订单的客户端。"""
    async with _lock:
        queues = _subscribers.get(order_id, set())
        if not queues:
            return
        
        event = {
            "order_id": order_id,
            "status": status.value,
            "timestamp": time.time(),
            **(data or {}),
        }
        
        # 发送到所有订阅者队列
        dead_queues = set()
        for q in queues:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # 客户端消费太慢，丢弃该订阅
                dead_queues.add(q)
        
        # 清理失效队列
        for q in dead_queues:
            queues.discard(q)


async def _subscribe(order_id: str) -> asyncio.Queue[dict]:
    """订阅一个订单的更新事件。"""
    q: asyncio.Queue[dict] = asyncio.Queue(maxsize=32)
    async with _lock:
        if order_id not in _subscribers:
            _subscribers[order_id] = set()
        _subscribers[order_id].add(q)
    return q


async def _unsubscribe(order_id: str, q: asyncio.Queue[dict]) -> None:
    """取消订阅。"""
    async with _lock:
        if order_id in _subscribers:
            _subscribers[order_id].discard(q)
            if not _subscribers[order_id]:
                del _subscribers[order_id]


async def _event_stream(order_id: str, initial_status: str) -> AsyncGenerator[str, None]:
    """生成 SSE 事件流。"""
    q = await _subscribe(order_id)
    
    try:
        # 发送初始状态
        yield _sse({"type": "init", "status": initial_status})
        
        while True:
            try:
                # 等待事件，60 秒超时发送心跳
                event = await asyncio.wait_for(q.get(), timeout=60.0)
                yield _sse(event)
            except asyncio.TimeoutError:
                # 心跳：保持连接活跃
                yield f": heartbeat {time.time()}\n\n"
    except asyncio.CancelledError:
        logger.info(f"SSE stream cancelled for order {order_id}")
    finally:
        await _unsubscribe(order_id, q)


@router.get("/orders/{order_id}/stream")
async def stream_order_updates(order_id: str, request: Request) -> StreamingResponse:
    """订阅订单状态更新的 SSE 流。
    
    客户端示例：
    ```javascript
    const events = new EventSource('/api/events/orders/{order_id}/stream');
    events.addEventListener('message', (e) => {
      const data = JSON.parse(e.data);
      console.log('Order update:', data);
    });
    ```
    """
    # 先查询当前订单状态
    order = store.get_order(order_id)
    if not order:
        # 订单不存在，返回一个立即结束的流
        async def not_found():
            yield _sse({"error": "Order not found"})
        return StreamingResponse(
            not_found(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # 禁用 Nginx 缓冲
            },
        )
    
    logger.info(f"Client subscribed to order {order_id}, current status: {order.status.value}")
    
    return StreamingResponse(
        _event_stream(order_id, order.status.value),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def publish_quick_pay(job_id: str, snapshot: dict[str, Any]) -> None:
    """Thread-safe non-blocking broadcast of a QuickPayJobOut snapshot to SSE listeners."""
    if not job_id:
        return
    with _qp_lock:
        targets = list(_qp_subscribers.get(job_id, ()))
    if not targets:
        return
    dead: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue[dict]]] = []
    event = {"type": "update", "timestamp": time.time(), "job": snapshot}

    def _enqueue(queue: asyncio.Queue[dict], item: dict[str, Any]) -> None:
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
                queue.put_nowait(item)
            except Exception:  # noqa: BLE001
                pass

    for loop, q in targets:
        if loop.is_closed():
            dead.append((loop, q))
            continue
        try:
            loop.call_soon_threadsafe(_enqueue, q, event)
        except RuntimeError:
            dead.append((loop, q))
    if dead:
        with _qp_lock:
            bucket = _qp_subscribers.get(job_id)
            if bucket is not None:
                for pair in dead:
                    bucket.discard(pair)
                if not bucket:
                    del _qp_subscribers[job_id]


async def _quick_pay_stream(
    job_id: str,
    initial: dict[str, Any],
    *,
    transform=None,
) -> AsyncGenerator[str, None]:
    loop = asyncio.get_running_loop()
    q: asyncio.Queue[dict] = asyncio.Queue(maxsize=32)
    pair = (loop, q)
    with _qp_lock:
        _qp_subscribers.setdefault(job_id, set()).add(pair)
    try:
        init_job = transform(initial) if callable(transform) else initial
        yield _sse({"type": "init", "timestamp": time.time(), "job": init_job})
        if str(initial.get("status") or "") in _QP_FINAL_STATUSES:
            return
        while True:
            try:
                item = await asyncio.wait_for(q.get(), timeout=25.0)
                if callable(transform) and isinstance(item, dict) and isinstance(item.get("job"), dict):
                    item = {**item, "job": transform(item["job"])}
                yield _sse(item)
                job_data = item.get("job") if isinstance(item, dict) else None
                if isinstance(job_data, dict) and str(job_data.get("status") or "") in _QP_FINAL_STATUSES:
                    return
            except asyncio.TimeoutError:
                yield f": heartbeat {time.time()}\n\n"
    except asyncio.CancelledError:
        logger.info("quick_pay SSE cancelled job=%s", job_id)
    finally:
        with _qp_lock:
            bucket = _qp_subscribers.get(job_id)
            if bucket is not None:
                bucket.discard(pair)
                if not bucket:
                    del _qp_subscribers[job_id]


@router.get("/quick-pay/{job_id}/stream")
async def stream_quick_pay_updates(job_id: str) -> StreamingResponse:
    """Subscribe to real-time QuickPay job stage/status updates via SSE."""
    from ..routers.quick_pay import _out
    from ..services.order_flow import get_job

    job = get_job(job_id)
    if job is None:
        async def not_found():
            yield _sse({"type": "error", "code": "not_found", "message": "quick-pay 任务不存在"})
        return StreamingResponse(
            not_found(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )
    initial = _out(job, admitted=True).model_dump()
    return StreamingResponse(
        _quick_pay_stream(job_id, initial),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )
