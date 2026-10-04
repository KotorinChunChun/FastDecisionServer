"""合成入力と制御可能な模擬バックエンド。実モデルは読み込まない。"""
import asyncio
import base64
import io
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PIL import Image
from fds.contracts import DecisionRequest, prepare_images

CONFIG = {
    "queue_size": 8,
    "default_model": "text-model",
    "default_device": "cpu",
    "models": {
        "text-model": {"name": "試験テキスト", "revision": "a" * 40, "images": False},
        "image-model": {"name": "試験画像", "revision": "b" * 40, "images": True},
    },
}


def payload(**overrides):
    value = {
        "model": "text-model", "device": "cpu", "state": "試験入力",
        "questions": {"ok": {"type": "noul", "instructions": "試験ですか？"}},
    }
    value.update(overrides)
    return value


def request(**overrides):
    return DecisionRequest(**payload(**overrides))


def image_uri(size=(4, 3), color=(255, 0, 0, 255), format="PNG"):
    image = Image.new("RGBA", size, color)
    if format in {"JPEG", "BMP"}:
        image = image.convert("RGB")
    data = io.BytesIO()
    image.save(data, format=format)
    mime = {"PNG": "png", "JPEG": "jpeg", "WEBP": "webp", "BMP": "bmp"}[format]
    return "data:image/" + mime + ";base64," + base64.b64encode(data.getvalue()).decode("ascii")


async def eventually(predicate, timeout=3):
    """完了条件を待つ。固定の長い sleep では試験を遅くしない。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("非同期処理が制限時間内に期待状態になりませんでした。")
        await asyncio.sleep(0.005)


class ControlledBackend:
    """指定入力をイベントで停止させ、実行順と同時実行数を測る。"""
    def __init__(self):
        self.loaded = None
        self.calls = []
        self.finished = []
        self.gates = {}
        self.failures = {}
        self.running = 0
        self.max_running = 0
        self.lock = threading.Lock()

    def block(self, state):
        gate = threading.Event()
        self.gates[state] = gate
        return gate

    def release_all(self):
        for gate in self.gates.values():
            gate.set()

    def warmup(self):
        self.loaded = ("text-model", "cpu")

    def decide(self, body):
        with self.lock:
            self.calls.append((body.state, body.model, body.device))
            self.running += 1
            self.max_running = max(self.max_running, self.running)
        try:
            gate = self.gates.get(body.state)
            if gate is not None and not gate.wait(8):
                raise RuntimeError("試験の停止イベントが解除されませんでした。")
            if body.state in self.failures:
                raise self.failures[body.state]
            images = prepare_images(body.images)
            answers = {}
            for key, question in body.questions.items():
                if question.type == "noul":
                    answers[key] = {"type": "noul", "noul": 0.75}
                elif question.type == "choice":
                    options = list(question.criteria)
                    answers[key] = {"type": "choice", "choice": options[0], "confidence": 1.0,
                                    "probabilities": {option: float(index == 0) for index, option in enumerate(options)}}
                else:
                    answers[key] = {"type": "score", "score": 1.0, "confidence": 1.0,
                                    "probabilities": {str(index): float(index == 1) for index in range(len(question.criteria))},
                                    "legend": {str(index): label for index, label in enumerate(question.criteria)}}
            device = "cpu" if body.device == "auto" else body.device
            self.loaded = (body.model, device)
            return {"model": body.model, "revision": CONFIG["models"][body.model]["revision"],
                    "device": device, "answers": answers, "load_ms": 0.0, "inference_ms": 1.0,
                    "usage": {"input_tokens": 12, "output_tokens": 0}, "provider": "fds",
                    "image_count": len(images)}
        finally:
            with self.lock:
                self.running -= 1
                self.finished.append(body.state)
