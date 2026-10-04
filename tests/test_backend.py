"""Jeff 接続部を模擬モジュールで確認し、GPU/CPUやモデル取得には依存しない。"""
import contextlib
import copy
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch

from tests.support import CONFIG, image_uri, request
from fds.backend import JeffBackend


class BackendTests(unittest.TestCase):
    def setUp(self):
        # ファイルfixtureもこの試験の配下だけに作り、終了時に削除する。
        self.temp = tempfile.TemporaryDirectory(prefix=".fixtures-", dir=Path(__file__).parent)
        self.root = Path(self.temp.name)
        self.config = copy.deepcopy(CONFIG)
        self.config["default_device"] = "auto"
        for name, entry in self.config["models"].items():
            entry.update(backend="jeff", checkpoint=f"models/{name}")
            folder = self.root / entry["checkpoint"]
            folder.mkdir(parents=True)
            (folder / "decision_config.json").write_text(json.dumps({"max_options": 64}), encoding="utf-8")
            (folder / ".fds-revision").write_text(entry["revision"], encoding="utf-8")
        self.torch = types.ModuleType("torch")
        self.torch.cuda = types.SimpleNamespace(is_available=Mock(return_value=True), empty_cache=Mock())
        self.torch.set_num_threads = Mock()
        self.torch.inference_mode = contextlib.nullcontext
        self.model = MagicMock()
        self.model.temperature = 1.0
        self.model.prepare.return_value = types.SimpleNamespace(input_tokens=10, counts=(2,))
        self.model.return_value.__truediv__.return_value.softmax.return_value.cpu.return_value.tolist.return_value = [[0.25, 0.75, 0.0]]
        self.loader = Mock(return_value=self.model)
        jeff = types.ModuleType("jeff")
        jeff.__path__ = []
        models = types.ModuleType("jeff.models")
        models.load_decision_model = self.loader
        model = types.ModuleType("jeff.model")
        self.answer = Mock(side_effect=lambda question, probabilities: {"type": question["type"], "received": probabilities})
        model.answer = self.answer
        self.modules = patch.dict("sys.modules", {"torch": self.torch, "jeff": jeff, "jeff.models": models, "jeff.model": model})
        self.modules.start()
        self.backend = JeffBackend(self.root, self.config)

    def tearDown(self):
        self.modules.stop()
        self.temp.cleanup()

    def test_explicit_cpu_overrides_available_cuda_and_passes_default_threads(self):
        device, duration = self.backend._load("text-model", "cpu")
        self.assertEqual(device, "cpu")
        self.assertGreaterEqual(duration, 0)
        self.assertEqual(self.loader.call_args.kwargs["device"], "cpu")
        self.assertEqual(self.loader.call_args.kwargs["cpu_threads"], 4)
        self.torch.set_num_threads.assert_called_once_with(4)
        self.assertEqual(self.backend.loaded, ("text-model", "cpu"))
        self.model.eval.assert_called_once()

    def test_auto_uses_configured_cpu_even_with_available_cuda(self):
        self.config["default_device"] = "cpu"
        self.backend._load("text-model", "auto")
        self.assertEqual(self.loader.call_args.kwargs["device"], "cpu")

    def test_auto_selects_cuda_when_available_and_cpu_when_not(self):
        self.backend._load("text-model", "auto")
        self.assertEqual(self.loader.call_args.kwargs["device"], "cuda")
        self.torch.cuda.is_available.return_value = False
        self.backend._load("text-model", "auto")
        self.assertEqual(self.loader.call_args.kwargs["device"], "cpu")
        self.assertEqual(self.loader.call_count, 2)

    def test_requested_cuda_does_not_silently_fall_back(self):
        self.torch.cuda.is_available.return_value = False
        with self.assertRaisesRegex(RuntimeError, "CUDA"):
            self.backend._load("text-model", "cuda")
        self.loader.assert_not_called()

    def test_same_model_device_reuses_load_and_changes_force_reload(self):
        self.backend._load("text-model", "cpu")
        self.backend._load("text-model", "cpu")
        self.assertEqual(self.loader.call_count, 1)
        self.backend._load("text-model", "cuda")
        self.backend._load("image-model", "cpu")
        self.assertEqual(self.loader.call_count, 3)
        self.assertEqual(self.backend.loaded, ("image-model", "cpu"))

    def test_missing_or_wrong_revision_fails_before_model_load(self):
        revision = self.root / self.config["models"]["text-model"]["checkpoint"] / ".fds-revision"
        revision.unlink()
        with self.assertRaisesRegex(RuntimeError, "revision"):
            self.backend._load("text-model", "cpu")
        revision.write_text("wrong-revision", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "revision"):
            self.backend._load("text-model", "cpu")
        self.loader.assert_not_called()

    def test_all_questions_use_one_model_device_and_normalized_images(self):
        self.model.prepare.side_effect = [types.SimpleNamespace(input_tokens=10, counts=(count,)) for count in [2, 2, 3]]
        body = request(model="image-model", device="cpu", images=[image_uri()], questions={
            "truth": {"type": "noul"}, "kind": {"type": "choice", "criteria": {"a": "甲", "b": "乙"}},
            "level": {"type": "score", "criteria": ["低", "中", "高"]},
        })
        result = self.backend.decide(body)
        self.assertEqual((result["model"], result["device"]), ("image-model", "cpu"))
        self.assertEqual(result["usage"], {"input_tokens": 30, "output_tokens": 0})
        self.assertEqual(self.loader.call_count, 1)
        self.assertEqual(self.loader.call_args.kwargs["device"], "cpu")
        self.assertEqual([call.args[0][0]["question"]["type"] for call in self.model.prepare.call_args_list], ["noul", "choice", "score"])
        self.assertEqual([len(call.args[1]) for call in self.answer.call_args_list], [2, 2, 3])
        for call in self.model.prepare.call_args_list:
            row = call.args[0][0]
            self.assertEqual(row["state"], body.state)
            self.assertEqual(row["images"][0].mode, "RGB")
        self.assertEqual([answer["type"] for answer in result["answers"].values()], ["noul", "choice", "score"])

    def test_token_limit_rejects_before_forward_pass(self):
        self.model.prepare.return_value = types.SimpleNamespace(input_tokens=8193, counts=(2,))
        with self.assertRaisesRegex(ValueError, "8192"):
            self.backend.decide(request())
        self.model.assert_not_called()
        self.answer.assert_not_called()

    def test_model_choice_limit_rejects_before_prepare(self):
        self.backend._load("text-model", "cpu")
        self.backend.limit = 2
        with self.assertRaisesRegex(ValueError, "上限"):
            self.backend.decide(request(questions={"kind": {"type": "choice", "criteria": {"a": "甲", "b": "乙", "c": "丙"}}}))
        self.model.prepare.assert_not_called()

    def test_nonfinite_probability_is_rejected(self):
        self.model.return_value.__truediv__.return_value.softmax.return_value.cpu.return_value.tolist.return_value = [[float("nan"), 0.5]]
        with self.assertRaisesRegex(RuntimeError, "不正な確率"):
            self.backend.decide(request())
        self.answer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
