"""HTTP境界を ASGITransport で通す。実TCPやモデル配布には依存しない。"""
import asyncio
import copy
import unittest

from tests.support import CONFIG, ControlledBackend, eventually, image_uri, payload
import httpx
from fds.server import create_app


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = ControlledBackend()
        self.stops = []
        self.app = create_app(copy.deepcopy(CONFIG), self.backend, control_token="test-control-token",
                              shutdown=lambda: self.stops.append(True))
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        await eventually(lambda: self.app.state.decisions.ready)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://127.0.0.1:8767")
        self.tasks = []

    async def asyncTearDown(self):
        self.backend.release_all()
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.client.aclose()
        await asyncio.wait_for(self.lifespan.__aexit__(None, None, None), timeout=5)

    def post_task(self, body, client=None):
        task = asyncio.create_task((client or self.client).post("/v1/decisions", json=body))
        self.tasks.append(task)
        return task

    async def test_health_models_and_openapi_are_available(self):
        response = await self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["service"], "FastDecisionServer")
        self.assertTrue(response.json()["ready"])
        self.assertEqual(response.json()["active"], 0)
        response = await self.client.get("/v1/models")
        models = {model["id"]: model for model in response.json()["models"]}
        self.assertEqual(models["text-model"]["modalities"], ["text"])
        self.assertEqual(models["image-model"]["modalities"], ["text", "image"])
        self.assertIn("cpu", models["text-model"]["devices"])
        response = await self.client.get("/openapi.json")
        self.assertIn("/v1/decisions", response.json()["paths"])

    async def test_noul_choice_score_results_and_metadata_are_returned(self):
        body = payload(questions={
            "truth": {"type": "noul"},
            "kind": {"type": "choice", "criteria": {"a": "甲", "b": "乙"}},
            "level": {"type": "score", "criteria": ["低", "中", "高"]},
        })
        response = await self.client.post("/v1/decisions", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["answers"]["truth"], {"type": "noul", "noul": 0.75})
        self.assertEqual(result["answers"]["kind"]["choice"], "a")
        self.assertEqual(result["answers"]["kind"]["probabilities"], {"a": 1.0, "b": 0.0})
        self.assertEqual(result["answers"]["level"]["score"], 1.0)
        self.assertEqual(result["answers"]["level"]["legend"], {"0": "低", "1": "中", "2": "高"})
        self.assertEqual((result["model"], result["device"], result["provider"]), ("text-model", "cpu", "fds"))
        self.assertEqual(len(result["request_id"]), 32)
        self.assertGreaterEqual(result["total_ms"], result["queue_ms"])
        self.assertEqual(self.backend.calls, [("試験入力", "text-model", "cpu")])

    async def test_omitted_model_uses_configured_default(self):
        body = payload()
        body.pop("model")
        response = await self.client.post("/v1/decisions", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["model"], CONFIG["default_model"])

    async def test_unknown_model_and_unsupported_images_rejected_before_backend(self):
        for body in [payload(model="unknown"), payload(images=[image_uri()])]:
            with self.subTest(model=body["model"]):
                response = await self.client.post("/v1/decisions", json=body)
                self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.backend.calls, [])

    async def test_valid_image_and_invalid_image_data_use_backend_validation(self):
        response = await self.client.post("/v1/decisions", json=payload(model="image-model", images=[image_uri()]))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["image_count"], 1)
        response = await self.client.post("/v1/decisions", json=payload(model="image-model", images=["data:image/png;base64,%%%"] ))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(len(self.backend.calls), 2)

    async def test_validation_does_not_reflect_request_text_or_base64(self):
        secret = "試験用の非反射確認マーカー"
        body = payload(state=secret, images=[secret], unexpected=secret)
        response = await self.client.post("/v1/decisions", json=body)
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(secret, response.text)
        self.assertNotIn("input", response.json()["detail"][0])
        self.assertEqual(self.backend.calls, [])

    async def test_body_limit_rejects_chunked_upload_without_content_length(self):
        async def chunks():
            for _ in range(23):
                yield b"x" * 1_000_000
        response = await self.client.post("/v1/decisions", content=chunks(), headers={"Content-Type": "application/json"})
        self.assertNotIn("content-length", response.request.headers)
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.backend.calls, [])

    async def test_backend_error_mapping(self):
        for index, (error, status) in enumerate([(ValueError("模擬入力不正"), 422), (TimeoutError("模擬期限切れ"), 504),
                                                (RuntimeError("模擬推論失敗"), 503), (OSError("模擬読込失敗"), 503)]):
            state = f"failure-{index}"
            self.backend.failures[state] = error
            with self.subTest(error=type(error).__name__):
                response = await self.client.post("/v1/decisions", json=payload(state=state))
                self.assertEqual(response.status_code, status, response.text)

    async def test_queue_full_is_http_429_and_health_remains_responsive(self):
        self.app.state.decisions.queue._maxsize = 1
        gate = self.backend.block("blocked")
        first = self.post_task(payload(state="blocked"))
        await eventually(lambda: len(self.backend.calls) == 1)
        second = self.post_task(payload(state="queued"))
        await eventually(lambda: self.app.state.decisions.queue.qsize() == 1)
        response = await self.client.post("/v1/decisions", json=payload(state="overflow"))
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["Retry-After"], "1")
        health = (await self.client.get("/health")).json()
        self.assertEqual((health["active"], health["queued"]), (1, 1))
        gate.set()
        responses = await asyncio.gather(first, second)
        self.assertTrue(all(response.status_code == 200 for response in responses))

    async def test_multiple_http_clients_keep_their_model_and_device(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://127.0.0.1:8767") as second_client:
            gate = self.backend.block("first-client")
            first = self.post_task(payload(state="first-client", model="text-model", device="cpu"))
            await eventually(lambda: len(self.backend.calls) == 1)
            second = self.post_task(payload(state="second-client", model="image-model", device="cuda"), second_client)
            await eventually(lambda: self.app.state.decisions.queue.qsize() == 1)
            self.assertEqual(self.backend.calls, [("first-client", "text-model", "cpu")])
            gate.set()
            first_result, second_result = [response.json() for response in await asyncio.gather(first, second)]
        self.assertEqual((first_result["model"], first_result["device"]), ("text-model", "cpu"))
        self.assertEqual((second_result["model"], second_result["device"]), ("image-model", "cuda"))
        self.assertEqual(self.backend.max_running, 1)

    async def test_cross_origin_is_rejected_and_same_origin_is_accepted(self):
        rejected = await self.client.post("/v1/decisions", json=payload(), headers={"Origin": "https://example.invalid"})
        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(self.backend.calls, [])
        accepted = await self.client.post("/v1/decisions", json=payload(), headers={"Origin": "http://127.0.0.1:8767"})
        self.assertEqual(accepted.status_code, 200, accepted.text)

    async def test_shutdown_requires_token_and_rejects_new_decisions(self):
        for headers in [{}, {"Authorization": "Bearer wrong"}]:
            response = await self.client.post("/admin/shutdown", headers=headers)
            self.assertEqual(response.status_code, 403)
        self.assertEqual(self.stops, [])
        response = await self.client.post("/admin/shutdown", headers={"Authorization": "Bearer test-control-token"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.stops, [True])
        response = await self.client.post("/v1/decisions", json=payload())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.backend.calls, [])


if __name__ == "__main__":
    unittest.main()
