"""推論スレッドを制御して、待ち列・破棄・単一実行の不変条件を測る。"""
import asyncio
import unittest

from tests.support import ControlledBackend, eventually, request
from fds.server import DecisionService
from fastapi import HTTPException


class DisconnectProbe:
    def __init__(self):
        self.disconnected = False
        self.checks = 0

    async def is_disconnected(self):
        self.checks += 1
        return self.disconnected


class DecisionServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = ControlledBackend()
        self.service = DecisionService(self.backend, capacity=8)
        self.tasks = []
        await self.service.start()
        await eventually(lambda: self.service.ready)

    async def asyncTearDown(self):
        self.backend.release_all()
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await asyncio.wait_for(self.service.stop(), timeout=5)

    def submit(self, body, probe=None):
        task = asyncio.create_task(self.service.submit(body, probe))
        self.tasks.append(task)
        return task

    async def start_blocked(self, state="blocked", timeout_seconds=30):
        gate = self.backend.block(state)
        task = self.submit(request(state=state, timeout_seconds=timeout_seconds))
        await eventually(lambda: any(call[0] == state for call in self.backend.calls))
        return gate, task

    async def test_queue_full_returns_429_and_retry_after(self):
        self.service.queue._maxsize = 1
        gate, first = await self.start_blocked()
        second = self.submit(request(state="queued"))
        await eventually(lambda: self.service.queue.qsize() == 1)
        with self.assertRaises(HTTPException) as caught:
            await self.service.submit(request(state="overflow"))
        self.assertEqual(caught.exception.status_code, 429)
        self.assertEqual(caught.exception.headers["Retry-After"], "1")
        self.assertEqual([call[0] for call in self.backend.calls], ["blocked"])
        gate.set()
        await asyncio.gather(first, second)
        self.assertEqual([call[0] for call in self.backend.calls], ["blocked", "queued"])

    async def test_priorities_order_pending_jobs_and_preserve_fifo(self):
        gate, first = await self.start_blocked()
        states = [("back1", "background"), ("normal1", "normal"), ("front1", "interactive"),
                  ("normal2", "normal"), ("front2", "interactive"), ("back2", "background")]
        tasks = [self.submit(request(state=state, priority=priority)) for state, priority in states]
        await eventually(lambda: self.service.queue.qsize() == 6)
        self.assertEqual([call[0] for call in self.backend.calls], ["blocked"])
        gate.set()
        await asyncio.gather(first, *tasks)
        self.assertEqual([call[0] for call in self.backend.calls],
                         ["blocked", "front1", "front2", "normal1", "normal2", "back1", "back2"])
        self.assertEqual(self.backend.max_running, 1)

    async def test_running_deadline_discards_result_but_holds_slot_until_backend_finishes(self):
        gate, expired = await self.start_blocked(timeout_seconds=1)
        follower = self.submit(request(state="follower", model="image-model", device="cuda"))
        await eventually(lambda: self.service.queue.qsize() == 1)
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(expired), timeout=3)
        self.assertEqual(self.service.active, 1)
        self.assertFalse(follower.done())
        self.assertEqual([call[0] for call in self.backend.calls], ["blocked"])
        self.assertEqual(self.backend.finished, [])
        gate.set()
        result = await asyncio.wait_for(follower, timeout=3)
        self.assertEqual((result["model"], result["device"]), ("image-model", "cuda"))
        self.assertIsInstance(expired.exception(), TimeoutError)
        self.assertEqual(self.backend.max_running, 1)
        self.assertEqual(self.service.active, 0)

    async def test_queued_deadline_is_discarded_without_calling_backend(self):
        gate, first = await self.start_blocked()
        expired = self.submit(request(state="expired", timeout_seconds=1))
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(expired), timeout=3)
        gate.set()
        await first
        await asyncio.wait_for(self.service.queue.join(), timeout=3)
        self.assertEqual([call[0] for call in self.backend.calls], ["blocked"])

    async def test_cancelled_running_task_does_not_release_inference_slot(self):
        gate, cancelled = await self.start_blocked()
        cancelled.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled
        follower = self.submit(request(state="follower"))
        await eventually(lambda: self.service.queue.qsize() == 1)
        self.assertEqual(self.service.active, 1)
        self.assertEqual([call[0] for call in self.backend.calls], ["blocked"])
        gate.set()
        await follower
        self.assertTrue(cancelled.cancelled())
        self.assertEqual(self.backend.max_running, 1)

    async def test_disconnect_discards_result_and_retains_slot(self):
        probe = DisconnectProbe()
        gate = self.backend.block("disconnected")
        disconnected = self.submit(request(state="disconnected"), probe)
        await eventually(lambda: len(self.backend.calls) == 1)
        probe.disconnected = True
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(disconnected, timeout=2)
        follower = self.submit(request(state="follower"))
        await eventually(lambda: self.service.queue.qsize() == 1)
        self.assertGreaterEqual(probe.checks, 2)
        self.assertEqual(self.service.active, 1)
        self.assertFalse(follower.done())
        self.assertEqual([call[0] for call in self.backend.calls], ["disconnected"])
        gate.set()
        await follower
        self.assertTrue(disconnected.cancelled())
        self.assertEqual(self.backend.max_running, 1)

    async def test_cancelled_queued_request_never_reaches_backend(self):
        gate, first = await self.start_blocked()
        queued = self.submit(request(state="cancelled"))
        await eventually(lambda: self.service.queue.qsize() == 1)
        queued.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await queued
        follower = self.submit(request(state="follower"))
        gate.set()
        await asyncio.gather(first, follower)
        self.assertEqual([call[0] for call in self.backend.calls], ["blocked", "follower"])

    async def test_request_model_and_device_stay_fixed_through_queue(self):
        gate, first = await self.start_blocked()
        expected = [("cpu-text", "text-model", "cpu"), ("gpu-image", "image-model", "cuda"),
                    ("auto-text", "text-model", "auto"), ("cpu-image", "image-model", "cpu")]
        jobs = [self.submit(request(state=state, model=model, device=device)) for state, model, device in expected]
        await eventually(lambda: self.service.queue.qsize() == len(expected))
        gate.set()
        results = await asyncio.gather(*jobs)
        await first
        self.assertEqual(self.backend.calls[1:], expected)
        self.assertEqual([(item["model"], item["device"]) for item in results],
                         [(model, "cpu" if device == "auto" else device) for _, model, device in expected])
        self.assertEqual(self.backend.max_running, 1)
        self.assertEqual(len({item["request_id"] for item in results}), len(results))
        self.assertTrue(all(item["queue_ms"] >= 0 and item["total_ms"] >= item["queue_ms"] for item in results))

    async def test_backend_failure_is_reported_and_worker_processes_next_request(self):
        self.backend.failures["failure"] = RuntimeError("模擬推論失敗")
        # 呼出側が例外の traceback を解放しても、worker の継続を壊さない。
        with self.assertRaisesRegex(RuntimeError, "模擬推論失敗"):
            await self.service.submit(request(state="failure"))
        result = await asyncio.wait_for(self.service.submit(request(state="success")), timeout=3)
        self.assertEqual(result["model"], "text-model")
        self.assertEqual([call[0] for call in self.backend.calls], ["failure", "success"])
        self.assertEqual(self.service.active, 0)


if __name__ == "__main__":
    unittest.main()


