"""关闭顺序：拒绝新工作、等待/取消保存、释放运行锁、关闭基础设施。"""

from __future__ import annotations
import asyncio
from collections.abc import Awaitable, Callable

Hook = Callable[[], Awaitable[None]]


class ShutdownGate:
    def __init__(self):
        self.closed = False
        self._tasks = {}
        self._zero = asyncio.Event()
        self._zero.set()

    def begin(self):
        if self.closed:
            raise RuntimeError("shutdown_gate_closed")
        task = asyncio.current_task()
        self._tasks[task] = self._tasks.get(task, 0) + 1
        self._zero.clear()

    async def end(self):
        task = asyncio.current_task()
        count = self._tasks[task] - 1
        if count:
            self._tasks[task] = count
        else:
            del self._tasks[task]
        if not self._tasks:
            self._zero.set()

    def close(self):
        self.closed = True

    async def wait_idle(self, timeout=None):
        try:
            await asyncio.wait_for(self._zero.wait(), timeout)
            return True
        except TimeoutError:
            return False

    async def cancel_pending(self):
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        # 等待 finally 落盘；不在保存完成前强制关闭数据库。
        await asyncio.gather(*tasks, return_exceptions=True)


class AdmissionMiddleware:
    """纯 ASGI 闸门，覆盖 SSE 响应和 finally 的整个生命周期。"""

    def __init__(self, app, gate):
        self.app, self.gate = app, gate

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if self.gate.closed:
            from starlette.responses import JSONResponse

            return await JSONResponse(
                {"detail": "runtime_shutting_down"}, status_code=503
            )(scope, receive, send)
        self.gate.begin()
        try:
            await self.app(scope, receive, send)
        finally:
            await self.gate.end()


class BorrowedResource:
    """框架使用公开上下文协议借用资源，组合根保留唯一关闭权。"""

    def __init__(self, resource):
        self.resource = resource

    def __getattr__(self, name):
        return getattr(self.resource, name)

    async def __aenter__(self):
        return self.resource

    async def __aexit__(self, *exc):
        return None


class ShutdownStack:
    def __init__(self):
        self._drain = []
        self._base = []
        self._locked = []
        self.order = []

    def push_drain(self, name: str, hook: Hook):
        self._drain.append((name, hook))

    def push_base(self, name: str, hook: Hook):
        self._base.append((name, hook))

    def push_locked_release(self, name: str, hook: Hook):
        self._locked.append((name, hook))

    async def teardown(self):
        errors = []
        # 运行锁在保存/消费者关闭之后释放，在它依赖的数据库连接池之前释放。
        for layer in (self._drain, self._locked, self._base):
            while layer:
                name, hook = layer.pop()
                try:
                    await hook()
                except Exception as exc:
                    self.order.append(f"{name}!:error")
                    errors.append(exc)
                else:
                    self.order.append(name)
        if errors:
            raise ExceptionGroup("runtime shutdown failed", errors)


async def run_shutdown_sequence(gate, stack, *, drain_timeout=30.0):
    gate.close()
    idle = await gate.wait_idle(drain_timeout)
    if not idle:
        await gate.cancel_pending()
    await stack.teardown()
    return {"idle": idle, "order": list(stack.order)}
