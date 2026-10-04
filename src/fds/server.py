"""HTTP受付と推論枠の所有権。取消は応答だけを捨て、計算を重ねない。"""
import asyncio
import hmac
import itertools
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError

from .contracts import DecisionRequest


class DecisionService:
    def __init__(self, backend, capacity=16):
        if not 1 <= capacity <= 256:
            raise ValueError("待ち列の上限は1～256です。")
        self.backend = backend
        self.queue = asyncio.PriorityQueue(maxsize=capacity)
        self.serial = itertools.count()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fds-inference")
        self.ready = False
        self.error = None
        self.active = 0
        self.completed = 0
        self.accepting = False

    async def start(self):
        self.accepting = True
        self.warmer = asyncio.create_task(self.warm())
        self.worker = asyncio.create_task(self.run())

    async def warm(self):
        try:
            await asyncio.get_running_loop().run_in_executor(self.executor, self.backend.warmup)
            self.ready = True
        except Exception as error:
            self.error = str(error)

    async def stop(self):
        self.accepting = False
        while not self.queue.empty():
            _, _, job = self.queue.get_nowait()
            if job is not None and not job[1].done():
                job[1].set_exception(RuntimeError("サーバーを停止しています。"))
            self.queue.task_done()
        await self.queue.put((99, next(self.serial), None))
        # warmup/推論はPythonタスクの取消で停止できないため、実完了を待つ。
        await self.worker
        self.executor.shutdown(wait=True)

    async def run(self):
        await self.warmer
        while True:
            _, _, job = await self.queue.get()
            try:
                if job is None:
                    return
                body, future, start, deadline = job
                if future.cancelled() or time.perf_counter() >= deadline:
                    if not future.done():
                        future.set_exception(TimeoutError("待ち時間が上限を超えました。"))
                    continue
                queued_ms = (time.perf_counter() - start) * 1000
                self.active = 1
                try:
                    result = await asyncio.get_running_loop().run_in_executor(self.executor, self.backend.decide, body)
                    self.ready, self.error = True, None
                    self.completed += 1
                    if not future.done():
                        if time.perf_counter() >= deadline:
                            future.set_exception(TimeoutError("推論の期限を超えました。"))
                        else:
                            future.set_result(result | {"request_id": uuid.uuid4().hex, "queue_ms": queued_ms,
                                                       "total_ms": (time.perf_counter() - start) * 1000})
                except Exception as error:
                    if not future.done():
                        future.set_exception(error.with_traceback(None))
                finally:
                    self.active = 0
            finally:
                self.queue.task_done()

    async def submit(self, body, request=None):
        if not self.accepting:
            raise RuntimeError("サーバーは受付を停止しています。")
        future = asyncio.get_running_loop().create_future()
        start = time.perf_counter()
        priority = {"interactive": 0, "normal": 1, "background": 2}[body.priority]
        try:
            self.queue.put_nowait((priority, next(self.serial), (body, future, start, start + body.timeout_seconds)))
        except asyncio.QueueFull:
            raise HTTPException(429, "判定の待ち列が満杯です。", headers={"Retry-After": "1"})
        try:
            while not future.done():
                remaining = start + body.timeout_seconds - time.perf_counter()
                if remaining <= 0:
                    raise TimeoutError("判定の期限を超えました。")
                if request is not None and await request.is_disconnected():
                    raise asyncio.CancelledError()
                await asyncio.wait({future}, timeout=min(.1, remaining))
            return future.result()
        finally:
            if not future.done():
                future.cancel()


def create_app(config, backend, control_token="", shutdown=None):
    service = DecisionService(backend, config.get("queue_size", 16))

    @asynccontextmanager
    async def lifespan(app):
        await service.start()
        try:
            yield
        finally:
            await service.stop()

    app = FastAPI(title="FastDecisionServer", version="0.1.0", lifespan=lifespan)
    app.state.decisions = service

    @app.middleware("http")
    async def boundary(request, call_next):
        # ブラウザーの外部ページからのloopback利用を許可しない。
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            return JSONResponse({"detail": "異なるオリジンからの要求は受け付けません。"}, status_code=403)
        if request.method == "POST":
            content = bytearray()
            async for chunk in request.stream():
                if len(content) + len(chunk) > 22_000_000:
                    return JSONResponse({"detail": "要求が22MBを超えました。"}, status_code=413)
                content.extend(chunk)
            request._body = bytes(content)
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        return JSONResponse({"detail": [{"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in error.errors()]}, status_code=422)

    @app.get("/health")
    async def health():
        return {"service": "FastDecisionServer", "alias": "fds", "version": "0.1.0", "ready": service.ready,
                "error": service.error, "active": service.active, "queued": service.queue.qsize(),
                "completed": service.completed, "loaded": backend.loaded, "pid": os.getpid(),
                "default_model": config.get("default_model", "jeff-qwen-2b"),
                "default_device": config.get("default_device", "auto"), "accepting": service.accepting}

    @app.get("/v1/models")
    async def models():
        return {"models": [{"id": k, "name": v["name"], "revision": v["revision"],
                            "modalities": ["text", "image"] if v.get("images") else ["text"],
                            "devices": ["auto", "cpu", "cuda"], "backend": v.get("backend", "jeff")}
                           for k, v in config["models"].items()]}

    @app.post("/v1/decisions")
    async def decide(body: DecisionRequest, request: Request):
        if "model" not in body.model_fields_set:
            body.model = config.get("default_model", body.model)
        if body.model not in config["models"]:
            raise HTTPException(422, "未登録のモデルです。")
        if body.images and not config["models"][body.model].get("images"):
            raise HTTPException(422, "このモデルは画像入力に対応していません。")
        try:
            return await service.submit(body, request)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        except TimeoutError as error:
            raise HTTPException(504, str(error)) from error
        except (RuntimeError, OSError) as error:
            raise HTTPException(503, str(error)) from error

    @app.post("/admin/shutdown")
    async def stop(request: Request):
        supplied = request.headers.get("authorization", "")
        if not control_token or not hmac.compare_digest(supplied, "Bearer " + control_token):
            raise HTTPException(403, "管理操作の認証に失敗しました。")
        service.accepting = False
        if shutdown:
            shutdown()
        return {"stopping": True}

    return app