"""起動準備と終了処理でも推論枠の所有を保つことを確認する。"""
import asyncio
import threading
import unittest

from tests.support import ControlledBackend, eventually, request
from fds.server import DecisionService


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_warmup_finishes_before_any_decision_starts(self):
        backend = ControlledBackend()
        warm_started = threading.Event()
        warm_finished = threading.Event()
        warm_gate = threading.Event()

        def warmup():
            warm_started.set()
            if not warm_gate.wait(8):
                raise RuntimeError("準備完了の試験イベントが解除されませんでした。")
            warm_finished.set()

        backend.warmup = warmup
        service = DecisionService(backend, capacity=1)
        submitted = None
        await service.start()
        try:
            await eventually(warm_started.is_set)
            submitted = asyncio.create_task(service.submit(request()))
            await eventually(lambda: service.queue.qsize() == 1)
            self.assertFalse(service.ready)
            self.assertEqual(service.active, 0)
            self.assertEqual(backend.calls, [])
            warm_gate.set()
            await asyncio.wait_for(submitted, timeout=3)
            self.assertTrue(warm_finished.is_set())
            self.assertTrue(service.ready)
            self.assertEqual(len(backend.calls), 1)
        finally:
            warm_gate.set()
            backend.release_all()
            if submitted is not None:
                await asyncio.gather(submitted, return_exceptions=True)
            await asyncio.wait_for(service.stop(), timeout=5)

    async def test_shutdown_rejects_new_and_queued_work_but_waits_for_active_inference(self):
        backend = ControlledBackend()
        gate = backend.block("running")
        service = DecisionService(backend, capacity=2)
        jobs = []
        stopping = None
        await service.start()
        try:
            first = asyncio.create_task(service.submit(request(state="running")))
            jobs.append(first)
            await eventually(lambda: len(backend.calls) == 1)
            queued = asyncio.create_task(service.submit(request(state="queued")))
            jobs.append(queued)
            await eventually(lambda: service.queue.qsize() == 1)
            stopping = asyncio.create_task(service.stop())
            await eventually(lambda: not service.accepting)
            with self.assertRaisesRegex(RuntimeError, "停止"):
                await service.submit(request(state="new"))
            with self.assertRaisesRegex(RuntimeError, "停止"):
                await queued
            self.assertFalse(stopping.done())
            self.assertEqual(service.active, 1)
            self.assertEqual(backend.finished, [])
            self.assertEqual([call[0] for call in backend.calls], ["running"])
            gate.set()
            await asyncio.wait_for(stopping, timeout=3)
            await first
            self.assertEqual(backend.finished, ["running"])
            self.assertEqual(service.active, 0)
            self.assertEqual(service.queue.qsize(), 0)
        finally:
            backend.release_all()
            await asyncio.gather(*jobs, return_exceptions=True)
            if stopping is not None:
                await asyncio.wait_for(stopping, timeout=5)
            else:
                await asyncio.wait_for(service.stop(), timeout=5)


if __name__ == "__main__":
    unittest.main()
