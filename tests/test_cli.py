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


if __name__ == "__main__":
    unittest.main()