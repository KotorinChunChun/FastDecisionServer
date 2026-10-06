"""実認証情報や外部通信を使わず、クラウド境界とローカルの分離を検証する。"""
import asyncio
import base64
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import httpx
from PIL import Image
from fds.cli import configuration
from fds.cloudflare import CloudflareBackend, CloudflareError
from fds.contracts import DecisionRequest, ModelOperation
from fds.routing import FdsBackend
from fds.server import create_app
from tests.support import ControlledBackend, eventually, image_uri, payload


ROOT = Path(__file__).resolve().parents[1]
ENV = {"CLOUDFLARE_ACCOUNT_ID": "a" * 32, "CLOUDFLARE_AUTH_TOKEN": "synthetic-test-token"}


def request(**changes):
    return DecisionRequest(**payload(model="cloudflare-clef", device="auto") | changes)


def result(model="clef"):
    return {"model": model, "answers": {"ok": {"type": "noul", "noul": .75}},
            "usage": {"input_tokens": 12, "output_tokens": 0}}


class CloudflareTests(unittest.TestCase):
    def setUp(self):
        self.config = configuration(ROOT)
        self.env = patch.dict(os.environ, ENV)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.calls = []

    def backend(self, response=None, status=200, error=None):
        def send(req):
            self.calls.append(req)
            if error:
                raise error
            return httpx.Response(status, json=result() if response is None else response)
        return CloudflareBackend(self.config, httpx.MockTransport(send))

    def test_endpoint_payload_auth_and_all_answer_types(self):
        questions = {"ok": {"type": "noul"},
                     "kind": {"type": "choice", "criteria": {"a": "甲", "b": "乙"}},
                     "level": {"type": "score", "criteria": ["低", "高"]}}
        upstream = result()
        upstream["answers"].update(kind={"type": "choice", "choice": "a", "confidence": .75,
                                         "probabilities": {"a": .75, "b": .25}},
                                   level={"type": "score", "score": .25, "confidence": .75,
                                          "probabilities": {"0": .75, "1": .25}})
        upstream["secret"] = "must-not-reflect"
        answer = self.backend({"success": True, "result": upstream}).decide(request(questions=questions))
        sent = self.calls[0]
        self.assertEqual(str(sent.url), "https://api.cloudflare.com/client/v4/accounts/" + "a" * 32 + "/ai/run/@cf/cloudflare/clef")
        self.assertEqual(sent.headers["authorization"], "Bearer synthetic-test-token")
        self.assertEqual(set(json.loads(sent.content)), {"model", "state", "questions"})
        self.assertEqual(answer["answers"]["level"]["legend"], {"0": "低", "1": "高"})
        self.assertEqual(answer["device"], "cloud")
        self.assertIsNone(answer["revision"])
        self.assertNotIn("must-not-reflect", json.dumps(answer))
        self.assertEqual(len(self.calls), 1)

    def test_flash_and_image_normalization(self):
        answer = self.backend(result("clef-flash")).decide(request(model="cloudflare-clef-flash", images=[image_uri((2048, 16))]))
        self.assertTrue(str(self.calls[0].url).endswith("/clef-flash"))
        data = json.loads(self.calls[0].content)
        with Image.open(io.BytesIO(base64.b64decode(data["images"][0].split(",")[1]))) as im:
            self.assertEqual(im.size, (1024, 8))
            self.assertEqual(im.mode, "RGB")
        self.assertEqual(answer["upstream_model"], "@cf/cloudflare/clef-flash")

    def test_invalid_input_never_sends(self):
        backend = self.backend()
        for change in [{"device": "cpu"}, {"device": "cuda"}, {"approval_token": "x" * 20},
                       {"questions": {"質問": {"type": "noul"}}}, {"images": ["https://example.invalid/image.png"]}]:
            with self.subTest(change=change), self.assertRaises((CloudflareError, ValueError)):
                backend.decide(request(**change))
        self.assertEqual(self.calls, [])

    def test_credentials_missing_or_invalid_never_send(self):
        for env in [{"CLOUDFLARE_ACCOUNT_ID": ""}, {"CLOUDFLARE_ACCOUNT_ID": "../other"},
                    {"CLOUDFLARE_AUTH_TOKEN": ""}, {"CLOUDFLARE_AUTH_TOKEN": "line\nbreak"}]:
            with patch.dict(os.environ, env):
                backend = self.backend()
                self.assertIsNotNone(backend.unavailable_reason())
                with self.assertRaises(CloudflareError) as error:
                    backend.decide(request())
                self.assertEqual(error.exception.code, "cloudflare_not_configured")
        self.assertEqual(self.calls, [])

    def test_http_failures_are_sanitized_and_never_retried(self):
        for status, code in [(401, "cloudflare_auth_failed"), (403, "cloudflare_auth_failed"),
                             (429, "cloudflare_rate_limited"), (500, "cloudflare_request_failed"),
                             (302, "cloudflare_request_failed")]:
            self.calls.clear()
            with self.subTest(status=status), self.assertRaises(CloudflareError) as caught:
                self.backend({"error": "private-upstream-message"}, status).decide(request())
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn("private-upstream-message", str(caught.exception))
            self.assertEqual(len(self.calls), 1)

    def test_transport_failure_never_reflects_url_or_retries(self):
        for error, code in [(httpx.ReadTimeout("private-url"), "cloudflare_timeout"),
                            (httpx.ConnectError("private-url"), "cloudflare_connection_failed")]:
            self.calls.clear()
            with self.assertRaises(CloudflareError) as caught:
                self.backend(error=error).decide(request())
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn("private-url", str(caught.exception))
            self.assertEqual(len(self.calls), 1)

    def test_invalid_responses_are_rejected(self):
        bad = [[], {"success": False, "result": result()}, result() | {"model": "other"},
               result() | {"answers": {}}, result() | {"usage": {"input_tokens": True, "output_tokens": 0}},
               result() | {"answers": {"ok": {"type": "noul", "noul": 2}}},
               result() | {"answers": {"ok": {"type": "choice", "noul": .5}}},
               result() | {"padding": "x" * (1024 ** 2)}]
        for value in bad:
            with self.subTest(keys=list(value)), self.assertRaises(CloudflareError) as caught:
                self.backend(value).decide(request())
            self.assertEqual(caught.exception.code, "cloudflare_invalid_response")

    def test_configuration_rejects_arbitrary_endpoint_and_secret_values(self):
        for changes in [{"cloudflare": {"endpoint": "https://example.invalid"}},
                        {"cloudflare": {"api_token": "secret"}},
                        {"cloudflare": {"api_token_env": "not a name"}},
                        {"models": self.config["models"] | {"bad": {"backend": "cloudflare", "cloudflare_model": "other"}}}]:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "config.json").write_text(json.dumps(self.config | changes), encoding="utf-8")
                with self.assertRaises(ValueError):
                    configuration(root)


class LocalStub(ControlledBackend):
    def status(self):
        return {"ready": self.loaded is not None, "loaded": self.loaded, "generation": 7, "loaded_models": []}

    def capabilities(self):
        return {"models": [], "devices": ["auto", "cpu"], "generation": 7}

    def validate_request(self, body):
        pass


class CloudflareApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = patch.dict(os.environ, ENV)
        self.env.start()
        self.config = configuration(ROOT)
        self.config["default_model"] = "cloudflare-clef"
        self.config["models"]["text-model"] = {"name": "試験用", "images": False}
        self.local = LocalStub()
        self.calls = []
        self.gate = None
        self.status = 200
        def send(req):
            self.calls.append(req)
            if self.gate is not None and not self.gate.wait(5):
                raise AssertionError("試験ゲート未解除")
            return httpx.Response(self.status, json=result())
        self.backend = FdsBackend(ROOT, self.config, local=self.local, transport=httpx.MockTransport(send))
        self.app = create_app(self.config, self.backend)
        self.life = self.app.router.lifespan_context(self.app)
        await self.life.__aenter__()
        await eventually(lambda: self.app.state.decisions.ready)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://127.0.0.1")

    async def asyncTearDown(self):
        if self.gate:
            self.gate.set()
        await self.client.aclose()
        await self.life.__aexit__(None, None, None)
        self.env.stop()

    async def test_cloud_startup_and_list_never_load_or_call_network(self):
        self.assertIsNone(self.local.loaded)
        models = (await self.client.get("/models")).json()
        self.assertEqual(models["capabilities_version"], 3)
        row = models["models"][0]
        self.assertTrue(row["available"])
        self.assertFalse(row["management_supported"])
        self.assertEqual(row["devices"], ["auto", "cloud"])
        self.assertTrue((await self.client.get("/health")).json()["cloud_configured"])
        self.assertEqual(self.calls, [])

    async def test_default_cloud_and_local_routing_preserves_local_state(self):
        response = await self.client.post("/v1/decisions", json={"state": "試験", "questions": {"ok": {"type": "noul"}}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["device"], "cloud")
        self.assertEqual(self.backend.status()["generation"], 7)
        self.assertEqual(self.local.calls, [])
        response = await self.client.post("/v1/decisions", json=payload())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.local.calls), 1)
        self.assertEqual(len(self.calls), 1)

    async def test_errors_before_and_inside_worker_and_unsupported_management(self):
        with patch.dict(os.environ, {"CLOUDFLARE_AUTH_TOKEN": ""}):
            response = await self.client.post("/v1/decisions", json=request().model_dump())
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["error"]["code"], "cloudflare_not_configured")
            row = (await self.client.get("/models")).json()["models"][0]
            self.assertFalse(row["available"])
        self.status = 403
        response = await self.client.post("/v1/decisions", json=request().model_dump())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "cloudflare_auth_failed")
        for action in ("load", "unload"):
            response = await self.client.post("/models/" + action, json={"model": "cloudflare-clef"})
            self.assertEqual(response.status_code, 422)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.local.calls, [])

    async def test_cancellation_holds_worker_until_cloud_call_returns(self):
        self.gate = threading.Event()
        service = self.app.state.decisions
        first = asyncio.create_task(service.submit(request()))
        await eventually(lambda: len(self.calls) == 1)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(service.submit(DecisionRequest(**payload())))
        try:
            await eventually(lambda: service.queue.qsize() == 1)
            self.assertEqual(service.active, 1)
            self.assertEqual(self.local.calls, [])
        finally:
            self.gate.set()
        await second
        self.assertEqual(len(self.local.calls), 1)
        self.assertEqual(len(self.calls), 1)
