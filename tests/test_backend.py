"""Jeff 接続部を模擬モジュールで確認し、GPU/CPUやモデル取得には依存しない。"""
import contextlib
import copy
import gc
import json
import tempfile
import types
import unittest
import weakref
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch

from tests.support import CONFIG, image_uri, request
from fds.backend import JeffBackend, TEXT_BATCH_TOKEN_LIMIT
from fds.management import ManagementError


class InputIds:
    """padding後の要素数と入力解放をモデルなしで確認する。"""
    def __init__(self, count):
        self.count = count

    def numel(self):
        return self.count


class BackendTests(unittest.TestCase):
    def setUp(self):
        # ファイルfixtureもこの試験の配下だけに作り、終了時に削除する。
        self.temp = tempfile.TemporaryDirectory(prefix=".fixtures-", dir=Path(__file__).parent)
        self.root = Path(self.temp.name)
        self.config = copy.deepcopy(CONFIG)
        self.config["default_device"] = "auto"
        for name, entry in self.config["models"].items():
            entry.update(backend="jeff", checkpoint=f"models/{name}", memory_mb={"cpu": {"ram": 1000, "vram": 0}, "cuda": {"ram": 1000, "vram": 2000}})
            folder = self.root / entry["checkpoint"]
            folder.mkdir(parents=True)
            (folder / 'model.safetensors').touch()
            (folder / "decision_config.json").write_text(json.dumps({"max_options": 64}), encoding="utf-8")
            (folder / ".fds-revision").write_text(entry["revision"], encoding="utf-8")
        self.torch = types.ModuleType("torch")
        self.torch.cuda = types.SimpleNamespace(is_available=Mock(return_value=True), empty_cache=Mock())
        self.torch.set_num_threads = Mock()
        self.torch.get_num_threads = Mock(return_value=4)
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
        self.backend.manager.memory = lambda device: {'ram': 100000, 'vram': 100000}

    def tearDown(self):
        self.modules.stop()
        self.temp.cleanup()

    def variable_batches(self, lengths=None, probabilities=None, on_prepare=None):
        """質問数・padding・選択肢数に追随する模擬入力とforward結果。"""
        lengths = lengths or {}
        probabilities = probabilities or {}
        self.logits_outputs = []

        def prepare(rows, max_length):
            if on_prepare:
                on_prepare(rows)
            sizes = [lengths.get(row["question"].get("instructions"), 10) for row in rows]
            if max(sizes) > max_length:
                raise ValueError(f"Question branch exceeds the {max_length}-token limit; no input was truncated.")
            counts = tuple(2 if row["question"]["type"] == "noul" else len(row["question"]["criteria"])
                           for row in rows)
            inputs = {"input_ids": InputIds(len(rows) * max(sizes))}
            return types.SimpleNamespace(inputs=inputs, counts=counts, input_tokens=sum(sizes), rows=rows)

        def forward(batch):
            distributions = []
            for row, count in zip(batch.rows, batch.counts, strict=True):
                values = probabilities.get(row["question"].get("instructions"), [1 / count] * count)
                distributions.append(list(values) + [0.0] * (64 - count))
            logits = MagicMock()
            logits.__truediv__.return_value.softmax.return_value.cpu.return_value.tolist.return_value = distributions
            self.logits_outputs.append(logits)
            return logits

        self.model.prepare.side_effect = prepare
        self.model.side_effect = forward

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
        with self.assertRaisesRegex(ManagementError, "CUDA"):
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
        with self.assertRaisesRegex(ManagementError, "revision"):
            self.backend._load("text-model", "cpu")
        revision.write_text("wrong-revision", encoding="utf-8")
        with self.assertRaisesRegex(ManagementError, "revision"):
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
        self.assertEqual(self.model.call_count, 3)
        self.assertEqual(result["execution"]["batch_sizes"], [1, 1, 1])
        self.assertEqual([len(call.args[0]) for call in self.model.prepare.call_args_list], [1, 1, 1])
        normalized_images = self.model.prepare.call_args_list[0].args[0][0]["images"]
        for call in self.model.prepare.call_args_list:
            self.assertIs(call.args[0][0]["images"], normalized_images)

    def test_mixed_text_questions_share_forward_and_preserve_ids_options_and_probabilities(self):
        probabilities = {"truth": [0.2, 0.8], "kind": [0.1, 0.7, 0.2], "level": [0.05, 0.15, 0.3, 0.5]}
        self.variable_batches(lengths={"truth": 11, "kind": 20, "level": 30}, probabilities=probabilities)
        self.model.temperature = 0.7
        body = request(questions={
            "truth-id": {"type": "noul", "instructions": "truth", "criteria": {"false": "いいえ", "true": "はい"}},
            "kind-id": {"type": "choice", "instructions": "kind", "criteria": {"c": "丙", "a": "甲", "b": "乙"}},
            "level-id": {"type": "score", "instructions": "level", "criteria": ["低", "中", "高", "最高"]},
        })
        result = self.backend.decide(body)
        self.assertEqual(list(result["answers"]), ["truth-id", "kind-id", "level-id"])
        self.assertEqual([value["received"] for value in result["answers"].values()], list(probabilities.values()))
        self.assertEqual([call.args[0] for call in self.answer.call_args_list],
                         [question.model_dump(exclude_none=True) for question in body.questions.values()])
        self.assertEqual(self.model.call_count, 1)
        self.assertEqual(self.model.prepare.call_count, 1)
        self.assertEqual(self.model.prepare.call_args.kwargs, {"max_length": 8192})
        for row in self.model.prepare.call_args.args[0]:
            self.assertEqual(row["state"], body.state)
            self.assertEqual(row["images"], [])
        self.assertEqual(result["usage"], {"input_tokens": 61, "output_tokens": 0})
        self.assertEqual(result["execution"], {"batch_sizes": [3], "cpu_threads": 4, "text_batch_token_limit": 1024})
        self.logits_outputs[0].__truediv__.assert_called_once_with(0.7)
        self.logits_outputs[0].__truediv__.return_value.softmax.assert_called_once_with(-1)

    def test_padding_limit_splits_before_forward_and_excludes_discarded_tokens(self):
        self.variable_batches(lengths={"long": 600, "short": 10})
        result = self.backend.decide(request(questions={
            "first": {"type": "noul", "instructions": "long"},
            "second": {"type": "noul", "instructions": "short"},
        }))
        self.assertEqual([len(call.args[0]) for call in self.model.prepare.call_args_list], [2, 1, 1])
        self.assertEqual(result["execution"]["batch_sizes"], [1, 1])
        self.assertEqual(result["usage"]["input_tokens"], 610)
        self.assertEqual(list(result["answers"]), ["first", "second"])
        self.assertEqual(self.model.call_count, 2)

    def test_padding_limit_allows_exact_limit_and_recursively_splits_larger_batches(self):
        self.variable_batches(lengths={"half": TEXT_BATCH_TOKEN_LIMIT // 2})
        result = self.backend.decide(request(questions={
            f"q{index}": {"type": "noul", "instructions": "half"} for index in range(8)
        }))
        self.assertEqual([len(call.args[0]) for call in self.model.prepare.call_args_list], [8, 4, 2, 2, 4, 2, 2])
        self.assertEqual(result["execution"]["batch_sizes"], [2, 2, 2, 2])
        self.assertEqual(result["usage"]["input_tokens"], 4096)
        self.assertEqual(list(result["answers"]), [f"q{index}" for index in range(8)])
        self.assertEqual(self.model.call_count, 4)

    def test_split_releases_parent_input_before_preparing_children(self):
        parent_input = []

        def check_released(rows):
            if parent_input:
                self.assertIsNone(parent_input[0]())

        self.variable_batches(lengths={"long": 600}, on_prepare=check_released)
        original_prepare = self.model.prepare.side_effect

        def remember_input(rows, max_length):
            batch = original_prepare(rows, max_length)
            if len(rows) > 1:
                parent_input.append(weakref.ref(batch.inputs["input_ids"]))
            return batch

        self.model.prepare.side_effect = remember_input
        result = self.backend.decide(request(questions={
            "first": {"type": "noul", "instructions": "long"},
            "second": {"type": "noul", "instructions": "long"},
        }))
        self.assertEqual(result["execution"]["batch_sizes"], [1, 1])

    def test_completed_decision_does_not_retain_request_until_cyclic_gc(self):
        body = request(model="image-model", images=[image_uri()])
        body_ref = weakref.ref(body)
        was_enabled = gc.isenabled()
        gc.disable()
        try:
            self.backend.decide(body)
            del body
            self.assertIsNone(body_ref())
        finally:
            if was_enabled:
                gc.enable()

    def test_batch_size_one_retains_sequential_processing(self):
        self.config["text_batch_size"] = 1
        self.variable_batches()
        result = self.backend.decide(request(questions={f"q{i}": {"type": "noul"} for i in range(3)}))
        self.assertEqual(self.model.call_count, 3)
        self.assertEqual([len(call.args[0]) for call in self.model.prepare.call_args_list], [1, 1, 1])
        self.assertEqual(result["execution"]["batch_sizes"], [1, 1, 1])
        self.assertEqual(result["usage"]["input_tokens"], 30)

    def test_configured_batch_size_limits_forward_rows(self):
        self.config["text_batch_size"] = 3
        self.variable_batches()
        result = self.backend.decide(request(questions={f"q{i}": {"type": "noul"} for i in range(8)}))
        self.assertEqual(result["execution"]["batch_sizes"], [3, 3, 2])
        self.assertEqual(result["usage"]["input_tokens"], 80)

    def test_single_long_question_keeps_existing_8192_token_allowance(self):
        self.variable_batches(lengths={"long": 8192})
        result = self.backend.decide(request(questions={"long-id": {"type": "noul", "instructions": "long"}}))
        self.assertEqual(self.model.call_count, 1)
        self.assertEqual(result["execution"]["batch_sizes"], [1])
        self.assertEqual(result["usage"]["input_tokens"], 8192)

    def test_long_question_is_split_from_short_question_without_truncation(self):
        self.variable_batches(lengths={"long": 8192, "short": 10})
        result = self.backend.decide(request(questions={
            "long-id": {"type": "noul", "instructions": "long"},
            "short-id": {"type": "noul", "instructions": "short"},
        }))
        self.assertEqual(result["execution"]["batch_sizes"], [1, 1])
        self.assertEqual(result["usage"]["input_tokens"], 8202)

    def test_each_question_limit_is_passed_to_prepare_without_truncation(self):
        self.variable_batches(lengths={"long": 8193})
        with self.assertRaisesRegex(ValueError, "8192"):
            self.backend.decide(request(questions={
                "short-id": {"type": "noul"}, "long-id": {"type": "noul", "instructions": "long"},
            }))
        self.model.assert_not_called()
        self.answer.assert_not_called()

    def test_all_choice_limits_are_checked_before_any_prepare(self):
        self.backend._load("text-model", "cpu")
        self.backend.limit = 2
        self.config["text_batch_size"] = 1
        with self.assertRaisesRegex(ValueError, "上限"):
            self.backend.decide(request(questions={
                "first": {"type": "noul"},
                "second": {"type": "choice", "criteria": {"a": "甲", "b": "乙", "c": "丙"}},
            }))
        self.model.prepare.assert_not_called()
        self.model.assert_not_called()

    def test_nonfinite_probability_in_later_batch_row_is_rejected(self):
        self.variable_batches(probabilities={"invalid": [float("nan"), 0.5]})
        with self.assertRaisesRegex(RuntimeError, "不正な確率"):
            self.backend.decide(request(questions={
                "first": {"type": "noul"}, "second": {"type": "noul", "instructions": "invalid"},
            }))
        self.assertEqual(self.model.call_count, 1)

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



    def test_cached_models_keep_their_own_option_limits(self):
        self.backend._load("text-model", "cpu")
        self.backend.limit = 2
        self.backend._load("image-model", "cpu")
        self.backend.limit = 10
        with self.assertRaisesRegex(ValueError, "上限"):
            self.backend.decide(request(questions={"kind": {"type": "choice", "criteria": {"a": "甲", "b": "乙", "c": "丙"}}}))
        self.assertEqual(self.backend.loaded, ("text-model", "cpu"))
        self.assertEqual(self.backend.limit, 2)
        self.assertEqual(self.loader.call_count, 2)

    def test_eval_failure_leaves_previous_cache_intact(self):
        self.backend._load("text-model", "cpu")
        previous = self.backend.model
        broken = MagicMock()
        broken.eval.side_effect = RuntimeError("模擬eval失敗")
        self.loader.return_value = broken
        with self.assertRaises(ManagementError) as error:
            self.backend._load("image-model", "cpu")
        self.assertEqual(error.exception.code, "model_load_failed")
        self.assertIs(self.backend.model, previous)
        self.assertEqual(self.backend.loaded, ("text-model", "cpu"))
        self.assertEqual(len(self.backend.manager.entries), 1)

    def test_installed_capabilities_distinguish_loaded_and_policy(self):
        self.backend._load("text-model", "cpu")
        self.backend.manager.policy["allow_load"] = False
        listing = self.backend.capabilities()
        row = next(row for row in listing["models"] if row["id"] == "text-model")
        self.assertTrue(row["available"])
        self.assertFalse(row["loadable"])
        self.assertFalse(row["load_allowed"])
        self.assertTrue(row["unload_allowed"])
        self.assertEqual(row["loaded_devices"], ["cpu"])
        self.assertEqual(row["status"]["value"], "ready")
        self.assertIn("cpu", row["devices"])
        self.backend.decide(request())
        with self.assertRaises(ManagementError) as error:
            self.backend._load("image-model", "cpu")
        self.assertEqual(error.exception.code, "load_not_allowed")


if __name__ == "__main__":
    unittest.main()
