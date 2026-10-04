"""HTTP受付と単一ワーカー。取消は応答だけを捨て、モデル操作を重ねない。"""
import asyncio
import hmac
import itertools
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from functools import partial

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError

from .contracts import DecisionRequest, ModelOperation
from .management import ManagementError
from . import __version__


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
        self.operation = None

    def refresh(self, error=None):
        if hasattr(self.backend, "status"):
            self.ready = self.backend.status()["ready"]
        else:
            self.ready = self.backend.loaded is not None
        self.error = str(error) if error is not None else None

    async def start(self):
        self.accepting = True
        self.warmer = asyncio.create_task(self.warm())
        self.worker = asyncio.create_task(self.run())

    async def warm(self):
        self.operation = {"action": "warming"}
        try:
            await asyncio.get_running_loop().run_in_executor(self.executor, self.backend.warmup)
            self.refresh()
        except Exception as error:
            self.refresh(error)
        finally:
            self.operation = None

    async def stop(self):
        self.accepting = False
        while not self.queue.empty():
            _, _, job = self.queue.get_nowait()
            if job is not None and not job[1].done():
                job[1].set_exception(RuntimeError("サーバーを停止しています。"))
            self.queue.task_done()
        await self.queue.put((99, next(self.serial), None))
        await self.worker
        self.executor.shutdown(wait=True)

    async def run(self):
        await self.warmer
        while True:
            _, _, job = await self.queue.get()
            try:
                if job is None:
                    return
                body, future, start, deadline, action = job
                if future.cancelled() or time.perf_counter() >= deadline:
                    if not future.done():
                        future.set_exception(TimeoutError("待ち時間が上限を超えました。"))
                    continue
                queued_ms = (time.perf_counter() - start) * 1000
                self.active = 1
                self.operation = {"action": action, "model": body.model, "device": body.device}
                try:
                    call = partial(self.backend.decide, body) if action == "inference" else partial(self.backend.manage, action, body)
                    result = await asyncio.get_running_loop().run_in_executor(self.executor, call)
                    self.refresh()
                    self.completed += action == "inference"
                    if not future.done():
                        if time.perf_counter() >= deadline:
                            future.set_exception(TimeoutError("処理の期限を超えました。"))
                        else:
                            future.set_result(result | {"request_id": uuid.uuid4().hex, "queue_ms": queued_ms,
                                                       "total_ms": (time.perf_counter() - start) * 1000})
                except Exception as error:
                    self.refresh(error)
                    if not future.done():
                        future.set_exception(error.with_traceback(None))
                finally:
                    self.active = 0
                    self.operation = None
            finally:
                self.queue.task_done()

    async def submit(self, body, request=None, action="inference"):
        if not self.accepting:
            raise RuntimeError("サーバーは受付を停止しています。")
        future = asyncio.get_running_loop().create_future()
        start = time.perf_counter()
        priority = {"interactive": 0, "normal": 1, "background": 2}[getattr(body, "priority", "normal")]
        try:
            self.queue.put_nowait((priority, next(self.serial), (body, future, start, start + body.timeout_seconds, action)))
        except asyncio.QueueFull:
            raise HTTPException(429, "処理の待ち列が満杯です。", headers={"Retry-After": "1"})
        try:
            while not future.done():
                remaining = start + body.timeout_seconds - time.perf_counter()
                if remaining <= 0:
                    raise TimeoutError("処理の期限を超えました。")
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

    app = FastAPI(title="FastDecisionServer", version=__version__, lifespan=lifespan)
    app.state.decisions = service

    @app.middleware("http")
    async def boundary(request, call_next):
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            return JSONResponse({"detail": "異なるオリジンからの要求は受け付けません。"}, status_code=403)
        if request.method == "POST":
            content = bytearray()
            limit = 16_384 if request.url.path in ("/models/load", "/models/unload") else 22_000_000
            async for chunk in request.stream():
                if len(content) + len(chunk) > limit:
                    return JSONResponse({"detail": "要求本文が上限を超えました。"}, status_code=413)
                content.extend(chunk)
            request._body = bytes(content)
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        return JSONResponse({"detail": [{"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in error.errors()]}, status_code=422)

    @app.exception_handler(ManagementError)
    async def management_error(request, error):
        return JSONResponse(error.response(), status_code=error.status)

    @app.get("/health")
    async def health():
        state = backend.status() if hasattr(backend, "status") else {}
        return {"service": "FastDecisionServer", "alias": "fds", "version": __version__,
                "ready": state.get("ready", service.ready), "running": service.accepting,
                "error": service.error or state.get("error"), "active": service.active, "queued": service.queue.qsize(),
                "completed": service.completed, "loaded": state.get("loaded", backend.loaded), "pid": os.getpid(),
                "loaded_models": state.get("loaded_models", []), "generation": state.get("generation", 0),
                "operation": state.get("operation") or service.operation,
                "model_management": state.get("model_management", {}),
                "default_model": config.get("default_model", "jeff-qwen-2b"),
                "default_device": config.get("default_device", "auto"), "accepting": service.accepting,
                "cpu_threads": config.get("cpu_threads", 4), "text_batch_size": config.get("text_batch_size", 8)}

    @app.get("/models")
    @app.get("/v1/models")
    async def models():
        if hasattr(backend, "capabilities"):
            return await asyncio.to_thread(backend.capabilities)
        rows = [{"id": k, "name": v["name"], "revision": v["revision"],
                 "modalities": ["text", "image"] if v.get("images") else ["text"],
                 "devices": ["auto", "cpu", "cuda"], "backend": v.get("backend", "jeff")}
                for k, v in config["models"].items()]
        return {"object": "list", "data": rows, "models": rows}

    async def execute(body, request, action="inference"):
        try:
            return await service.submit(body, request, action)
        except (HTTPException, ManagementError):
            raise
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        except TimeoutError as error:
            raise HTTPException(504, str(error)) from error
        except (RuntimeError, OSError) as error:
            raise HTTPException(503, str(error)) from error
        except Exception:
            raise HTTPException(500, "処理に失敗しました。") from None

    @app.post("/models/load")
    async def load_model(body: ModelOperation, request: Request):
        return await execute(body, request, "load")

    @app.post("/models/unload")
    async def unload_model(body: ModelOperation, request: Request):
        return await execute(body, request, "unload")

    @app.post("/v1/decisions")
    async def decide(body: DecisionRequest, request: Request):
        if "model" not in body.model_fields_set:
            body.model = config.get("default_model", body.model)
        if body.model not in config["models"]:
            raise ManagementError("model_unknown", "未登録のモデルです。", 422)
        if body.images and not config["models"][body.model].get("images"):
            raise HTTPException(422, "このモデルは画像入力に対応していません。")
        if hasattr(backend, "validate_request"):
            try:
                await asyncio.to_thread(backend.validate_request, body)
            except ManagementError:
                raise
            except ValueError as error:
                raise HTTPException(422, str(error)) from error
        return await execute(body, request)

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
