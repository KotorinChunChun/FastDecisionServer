"""管理HTTPと推論キューの合同試験。モデルもCPU/GPUも模擬する。"""
import asyncio
import copy
import threading
import unittest

import httpx

from tests.support import CONFIG, ControlledBackend, eventually, payload, request
from fds.contracts import ModelOperation
from fds.management import ManagementError, Resident, ResidentModels
from fds.server import DecisionService, create_app


class ManagedBackend(ControlledBackend):
    def __init__(self):
        super().__init__()
        self.config = copy.deepcopy(CONFIG)
        self.manager = ResidentModels({"model_management": {"max_loaded_models": 1, "ram_reserve_mb": 0}},
                                      self.create_model, lambda: None,
                                      lambda device: {"ram": 100000, "vram": 100000})
        self.load_failure = False

    def create_model(self, key, estimate):
        if self.load_failure:
            raise RuntimeError("模擬ロード失敗")
        return Resident(object(), 64, "revision", estimate)

    def status(self):
        return self.manager.snapshot()

    def target(self, body):
        if body.model not in self.config["models"]:
            raise ManagementError("model_unknown", "未登録", 422)
        return (body.model, "cpu" if body.device == "auto" else body.device)

    def manage(self, action, body):
        with self.lock:
            self.calls.append((action, body.model, body.device))
            self.running += 1
            self.max_running = max(self.max_running, self.running)
        try:
            gate = self.gates.get(action)
            if gate and not gate.wait(8):
                raise RuntimeError("試験待機の解除失敗")
            key = self.target(body)
            if action == "unload":
                return self.manager.unload(key, body.approval_token)
            return self.manager.load(key, {"ram": 1000, "vram": 2000 if key[1] == "cuda" else 0},
                                     body.auto_unload, body.approval_token)
        finally:
            with self.lock:
                self.running -= 1
                self.finished.append(action)

    def warmup(self):
        self.manager.load(("text-model", "cpu"), {"ram": 1000, "vram": 0})
        super().warmup()

    def decide(self, body):
        key = self.target(body)
        self.manager.load(key, {"ram": 1000, "vram": 2000 if key[1] == "cuda" else 0},
                          body.auto_unload, body.approval_token)
        return super().decide(body)

    def capabilities(self):
        state = self.manager.snapshot()
        rows = [{"id": key, "available": True, "loadable": self.manager.policy["allow_load"],
                 "loaded_devices": [row["device"] for row in state["loaded_models"] if row["model"] == key],
                 "status": {"value": "ready" if any(row["model"] == key for row in state["loaded_models"]) else "unloaded"}}
                for key in self.config["models"]]
        return {"object": "list", "data": rows, "models": rows, **state}


class ManagementApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = ManagedBackend()
        self.app = create_app(self.backend.config, self.backend)
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        await eventually(lambda: self.app.state.decisions.ready)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://127.0.0.1:8767")

    async def asyncTearDown(self):
        self.backend.release_all()
        await self.client.aclose()
        await asyncio.wait_for(self.lifespan.__aexit__(None, None, None), 5)

    async def test_load_requires_approval_and_returns_compatible_list_state(self):
        body = {"model": "image-model", "device": "cpu"}
        response = await self.client.post("/models/load", json=body)
        self.assertEqual(response.status_code, 409, response.text)
        error = response.json()
        self.assertEqual(error["error"]["code"], "approval_required")
        self.assertEqual(error["detail"]["code"], "approval_required")
        token = error["detail"]["approval"]["token"]
        before = (await self.client.get("/health")).json()
        self.assertEqual(before["loaded_models"][0]["model"], "text-model")
        result = await self.client.post("/models/load", json=body | {"approval_token": token})
        self.assertEqual(result.status_code, 200, result.text)
        value = result.json()
        for field in ["success", "model", "device", "load_ms", "unloaded", "loaded_models", "generation"]:
            self.assertIn(field, value)
        self.assertEqual(value["unloaded"], [{"model": "text-model", "device": "cpu"}])
        self.assertNotIn(token, result.text)
        for path in ["/models", "/v1/models"]:
            listing = (await self.client.get(path)).json()
            self.assertEqual(listing["object"], "list")
            self.assertEqual(listing["models"], listing["data"])
            self.assertEqual(listing["loaded_models"], value["loaded_models"])

    async def test_inference_cannot_bypass_management_approval(self):
        body = payload(model="image-model", device="cuda")
        response = await self.client.post("/v1/decisions", json=body)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "approval_required")
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(list(self.backend.manager.entries), [("text-model", "cpu")])
        token = response.json()["detail"]["approval"]["token"]
        response = await self.client.post("/v1/decisions", json=body | {"approval_token": token})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.backend.calls), 1)

    async def test_policy_and_invalid_management_are_distinguishable(self):
        self.backend.manager.policy["allow_load"] = False
        response = await self.client.post("/models/load", json={"model": "image-model"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"]["code"], "load_not_allowed")
        for body in [{"model": "unknown"}, {"model": "text-model", "device": "gpu"},
                     {"model": "text-model", "auto_unload": "true"},
                     {"model": "text-model", "timeout_seconds": 301},
                     {"model": "text-model", "approval_token": "short"}]:
            response = await self.client.post("/models/load", json=body)
            self.assertEqual(response.status_code, 422, response.text)
        response = await self.client.post("/models/load", content=b"x" * 16385)
        self.assertEqual(response.status_code, 413)

    async def test_empty_after_unload_or_failed_replacement_is_reported_not_ready(self):
        body = {"model": "image-model", "device": "cpu"}
        response = await self.client.post("/models/load", json=body)
        token = response.json()["detail"]["approval"]["token"]
        self.backend.load_failure = True
        failure = await self.client.post("/models/load", json=body | {"approval_token": token})
        self.assertEqual(failure.status_code, 503, failure.text)
        self.assertEqual(failure.json()["detail"]["code"], "model_load_failed")
        health = (await self.client.get("/health")).json()
        self.assertFalse(health["ready"])
        self.assertTrue(health["running"])
        self.assertIsNone(health["loaded"])
        self.assertEqual(health["loaded_models"], [])
        self.assertTrue(health["error"])
        self.backend.load_failure = False
        self.assertEqual((await self.client.post("/models/load", json=body)).status_code, 200)
        response = await self.client.post("/models/unload", json=body)
        self.assertEqual(response.status_code, 409)
        token = response.json()["detail"]["approval"]["token"]
        result = await self.client.post("/models/unload", json=body | {"approval_token": token})
        self.assertEqual(result.status_code, 200)
        health = (await self.client.get("/health")).json()
        self.assertFalse(health["ready"])
        self.assertIsNone(health["error"])


class ManagementQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = ManagedBackend()
        self.backend.manager.policy["max_loaded_models"] = 3
        self.service = DecisionService(self.backend, 8)
        self.tasks = []
        await self.service.start()
        await eventually(lambda: self.service.ready)

    async def asyncTearDown(self):
        self.backend.release_all()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await asyncio.wait_for(self.service.stop(), 5)

    def submit(self, body, action="inference"):
        task = asyncio.create_task(self.service.submit(body, action=action))
        self.tasks.append(task)
        return task

    async def test_timed_out_load_holds_worker_until_actual_completion(self):
        gate = self.backend.block("load")
        pending = self.submit(ModelOperation(model="image-model", device="cpu", timeout_seconds=1), "load")
        await eventually(lambda: len(self.backend.calls) == 1)
        follower = self.submit(request(state="follower"))
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(pending), 3)
        self.assertEqual(self.service.active, 1)
        self.assertFalse(follower.done())
        self.assertNotIn(("image-model", "cpu"), self.backend.manager.entries)
        gate.set()
        await asyncio.wait_for(follower, 3)
        self.assertIn(("image-model", "cpu"), self.backend.manager.entries)
        self.assertEqual(self.backend.max_running, 1)

    async def test_queued_cancelled_management_never_changes_state(self):
        gate = self.backend.block("inference-block")
        running = self.submit(request(state="inference-block"))
        await eventually(lambda: len(self.backend.calls) == 1)
        load = self.submit(ModelOperation(model="image-model"), "load")
        await eventually(lambda: self.service.queue.qsize() == 1)
        load.cancel()
        gate.set()
        await running
        await self.service.queue.join()
        self.assertEqual(list(self.backend.manager.entries), [("text-model", "cpu")])

    async def test_generation_is_checked_when_approved_job_reaches_worker(self):
        self.backend.manager.policy["max_loaded_models"] = 1
        body = ModelOperation(model="image-model", device="cpu")
        with self.assertRaises(ManagementError) as caught:
            await self.service.submit(body, action="load")
        token = caught.exception.approval["token"]
        gate = self.backend.block("load")
        old = self.submit(ModelOperation(model="text-model", device="cpu"), "load")
        await eventually(lambda: len(self.backend.calls) == 2)
        approved = self.submit(body.model_copy(update={"approval_token": token}), "load")
        await eventually(lambda: self.service.queue.qsize() == 1)
        # 外部状態の変更を模擬。実装側はワーカー開始時に再照合する。
        self.backend.manager.generation += 1
        gate.set()
        await old
        with self.assertRaises(ManagementError) as stale:
            await approved
        self.assertEqual(stale.exception.code, "approval_required")
        self.assertEqual(list(self.backend.manager.entries), [("text-model", "cpu")])


if __name__ == "__main__":
    unittest.main()
