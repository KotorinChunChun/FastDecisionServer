"""配布設定の起動前検証。登録済み全モデルが受理されることを守る。"""
import json
from pathlib import Path
import tempfile
import unittest

from fds.cli import configuration


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.sample = json.loads((Path(__file__).resolve().parents[1] / "config.json").read_text(encoding="utf-8"))

    def configured(self, changes=None):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "config.json").write_text(json.dumps(self.sample | (changes or {})), encoding="utf-8")
            return configuration(root)

    def test_shipped_config_including_decimal_model_name_is_valid(self):
        config = self.configured()
        self.assertIn("jeff-qwen-0.8b", config["models"])
        self.assertEqual(config["default_model"], "jeff-qwen-2b")

    def test_invalid_service_bounds_are_rejected(self):
        for changes in [{"host": "0.0.0.0"}, {"default_device": "gpu"}, {"port": 0}, {"port": 65536},
                        {"queue_size": 0}, {"cpu_threads": True}, {"default_model": "missing"}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.configured(changes)

    def test_checkpoint_outside_models_is_rejected(self):
        self.sample["models"]["jeff-qwen-2b"]["checkpoint"] = "../outside"
        with self.assertRaisesRegex(ValueError, "models"):
            self.configured()



    def test_model_management_and_memory_estimates_are_validated(self):
        for policy in [{"allow_load": "yes"}, {"allow_unload": 1}, {"max_loaded_models": 0},
                       {"ram_reserve_mb": -1}, {"vram_reserve_mb": True}, {"approval_ttl_seconds": 0},
                       {"unknown": True}]:
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                self.configured({"model_management": policy})
        for estimate in [{"ram": -1, "vram": 0}, {"ram": float("nan"), "vram": 0},
                         {"ram": 1000, "vram": 1}, {"ram": True, "vram": 0},
                         {"ram": 1000}]:
            self.sample["models"]["jeff-qwen-2b"]["memory_mb"] = {"cpu": estimate}
            with self.subTest(estimate=estimate), self.assertRaises(ValueError):
                self.configured()

    def test_partial_management_policy_uses_documented_defaults(self):
        value = self.configured({"model_management": {"allow_load": False}})
        self.assertFalse(value["model_management"]["allow_load"])
        self.assertTrue(value["model_management"]["allow_unload"])
        self.assertEqual(value["model_management"]["max_loaded_models"], 3)


if __name__ == "__main__":
    unittest.main()