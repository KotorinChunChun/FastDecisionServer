"""実モデルを使わず、容量計画・承認・LRU・失敗時の常駐維持を検証する。"""
import unittest

from fds.management import DEFAULT_POLICY, ManagementError, Resident, ResidentModels


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.capacity = {"ram": 20000, "vram": 20000}
        self.loaded_calls = []
        self.failure = None
        self.manager = ResidentModels({"model_management": {
            "ram_reserve_mb": 100, "vram_reserve_mb": 100, "max_loaded_models": 3, "allow_auto_unload": True
        }}, self.load_model, lambda: None, self.memory)
        self.estimate = {"ram": 1000, "vram": 0}

    def memory(self, device):
        return {resource: self.capacity[resource] - sum(row.memory[resource] for row in self.manager.entries.values())
                for resource in ("ram", "vram")}

    def load_model(self, key, estimate):
        self.loaded_calls.append(key)
        if self.failure:
            raise self.failure
        return Resident(object(), 64, "revision", estimate)

    def load(self, name, device="cpu", **kwargs):
        estimate = {"ram": 1000, "vram": 2000 if device == "cuda" else 0}
        return self.manager.load((name, device), estimate, **kwargs)

    def error(self, code, call):
        with self.assertRaises(ManagementError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def approval(self, call):
        return self.error("approval_required", call).approval

    def test_free_addition_reuse_and_independent_devices_need_no_approval(self):
        first = self.load("a")
        self.load("a", "cuda")
        self.load("a")
        self.assertEqual(len(self.manager.entries), 2)
        self.assertEqual(self.loaded_calls, [("a", "cpu"), ("a", "cuda")])
        self.assertEqual(first["unloaded"], [])
        self.assertEqual(self.manager.snapshot()["generation"], 2)

    def test_capacity_challenge_keeps_state_and_consumed_token_cannot_be_reused(self):
        self.manager.policy["max_loaded_models"] = 1
        self.load("a")
        before = self.manager.snapshot()
        approval = self.approval(lambda: self.load("b"))
        self.assertEqual(self.manager.snapshot(), before)
        self.assertEqual(approval["unload"], [{"model": "a", "device": "cpu"}])
        self.assertEqual((approval["action"], approval["model"], approval["device"]), ("load", "b", "cpu"))
        result = self.load("b", token=approval["token"])
        self.assertEqual(result["unloaded"], approval["unload"])
        self.assertEqual(list(self.manager.entries), [("b", "cpu")])
        self.error("approval_invalid", lambda: self.load("b", token=approval["token"]))
        self.assertEqual(list(self.manager.entries), [("b", "cpu")])

    def test_cpu_gpu_switch_needs_approval_only_if_it_evicts(self):
        self.manager.policy["max_loaded_models"] = 1
        self.load("a")
        approval = self.approval(lambda: self.load("a", "cuda"))
        self.assertEqual(approval["unload"], [{"model": "a", "device": "cpu"}])
        self.load("a", "cuda", token=approval["token"])
        self.assertEqual(list(self.manager.entries), [("a", "cuda")])

    def test_unload_requires_approval_noop_does_not(self):
        self.load("a")
        no_op = self.manager.unload(("missing", "cpu"))
        self.assertEqual(no_op["unloaded"], [])
        approval = self.approval(lambda: self.manager.unload(("a", "cpu")))
        self.assertEqual(approval["action"], "unload")
        self.manager.unload(("a", "cpu"), approval["token"])
        self.assertFalse(self.manager.snapshot()["ready"])
        self.assertIsNone(self.manager.snapshot()["loaded"])
        self.error("approval_invalid", lambda: self.manager.unload(("a", "cpu"), approval["token"]))

    def test_wrong_target_action_or_device_does_not_change_residents(self):
        for changed in ("target", "action", "device"):
            self.manager.policy["max_loaded_models"] = 1
            self.load("a")
            token = self.approval(lambda: self.load("b"))["token"]
            call = {"target": lambda: self.load("c", token=token),
                    "action": lambda: self.manager.unload(("b", "cpu"), token),
                    "device": lambda: self.load("b", "cuda", token=token)}[changed]
            self.error("approval_invalid", call)
            self.assertEqual(list(self.manager.entries), [("a", "cpu")])

    def test_expired_or_stale_approval_returns_fresh_challenge_without_unloading(self):
        self.manager.policy["max_loaded_models"] = 1
        self.load("a")
        token = self.approval(lambda: self.load("b"))["token"]
        self.manager._challenges[token]["deadline"] = 0
        fresh = self.approval(lambda: self.load("b", token=token))
        self.assertNotEqual(fresh["token"], token)
        self.assertEqual(list(self.manager.entries), [("a", "cpu")])
        self.manager.generation += 1
        renewed = self.approval(lambda: self.load("b", token=fresh["token"]))
        self.assertEqual(renewed["generation"], self.manager.generation)
        self.assertEqual(list(self.manager.entries), [("a", "cpu")])

    def test_lru_is_recalculated_at_execution_and_never_expands_approved_victims(self):
        self.manager.policy["max_loaded_models"] = 2
        self.load("a")
        self.load("b")
        original = self.approval(lambda: self.load("c"))
        self.assertEqual(original["unload"][0]["model"], "a")
        self.load("a")
        changed = self.approval(lambda: self.load("c", token=original["token"]))
        self.assertEqual(changed["unload"][0]["model"], "b")
        self.assertEqual(set(self.manager.entries), {("a", "cpu"), ("b", "cpu")})

    def test_policy_and_auto_unload_are_rechecked_despite_approval(self):
        self.manager.policy["max_loaded_models"] = 1
        self.load("a")
        token = self.approval(lambda: self.load("b"))["token"]
        self.error("insufficient_capacity", lambda: self.load("b", auto_unload=False, token=token))
        self.manager.policy["allow_unload"] = False
        self.error("unload_not_allowed", lambda: self.load("b", token=token))
        self.error("unload_not_allowed", lambda: self.manager.unload(("a", "cpu"), token))
        self.manager.policy["allow_load"] = False
        self.error("load_not_allowed", lambda: self.load("b", token=token))
        self.load("a")
        self.assertEqual(list(self.manager.entries), [("a", "cpu")])

    def test_ram_and_vram_have_separate_reserves_and_insufficient_is_non_destructive(self):
        self.capacity["ram"] = 2200
        self.capacity["vram"] = 4200
        self.load("a", "cuda")
        self.load("b", "cuda")
        before = list(self.manager.entries)
        approval = self.approval(lambda: self.load("c", "cuda"))
        self.assertEqual(len(approval["unload"]), 1)
        self.capacity["vram"] = 5000
        self.error("insufficient_capacity", lambda: self.manager.load(("c", "cuda"), {"ram": 1000, "vram": 6000}, token=approval["token"]))
        self.assertEqual(list(self.manager.entries), before)
        self.assertEqual(len(self.loaded_calls), 2)

    def test_unknown_memory_or_estimate_rejects_without_unloading(self):
        self.load("a")
        self.manager.memory = lambda device: {"ram": None, "vram": 5000}
        self.error("memory_unavailable", lambda: self.load("b"))
        self.error("memory_estimate_missing", lambda: self.manager.load(("b", "cpu"), None))
        self.assertEqual(list(self.manager.entries), [("a", "cpu")])

    def test_failure_keeps_unrelated_residents_and_does_not_fallback_evict(self):
        self.load("a")
        self.failure = RuntimeError("模擬OOM")
        error = self.error("model_load_failed", lambda: self.load("b"))
        self.assertEqual(error.status, 503)
        self.assertEqual(list(self.manager.entries), [("a", "cpu")])
        self.assertTrue(self.manager.snapshot()["ready"])
        self.assertIsNone(self.manager.snapshot()["operation"])

    def test_failed_replacement_leaves_only_explicitly_approved_evictions(self):
        self.manager.policy["max_loaded_models"] = 1
        self.load("a")
        token = self.approval(lambda: self.load("b"))["token"]
        self.failure = RuntimeError("模擬OOM")
        self.error("model_load_failed", lambda: self.load("b", token=token))
        self.assertFalse(self.manager.snapshot()["ready"])
        self.assertEqual(list(self.manager.entries), [])
        self.failure = None
        self.load("b")
        self.assertTrue(self.manager.snapshot()["ready"])

    def test_actual_memory_is_checked_again_after_approved_eviction(self):
        self.manager.policy["max_loaded_models"] = 1
        self.load("a")
        token = self.approval(lambda: self.load("b"))["token"]
        self.manager.disposer = lambda: self.capacity.update(ram=500)
        self.error("insufficient_capacity", lambda: self.load("b", token=token))
        self.assertEqual(self.loaded_calls, [("a", "cpu")])
        self.assertFalse(self.manager.snapshot()["ready"])

    def test_default_policy_keeps_all_three_models_on_both_devices_and_reuses_them(self):
        self.manager.policy = dict(DEFAULT_POLICY)
        for name in ("a", "b", "c"):
            for device in ("cpu", "cuda"):
                self.assertEqual(self.load(name, device)["unloaded"], [])
        before = {key: value.model for key, value in self.manager.entries.items()}
        self.assertEqual(len(before), 6)
        for key in reversed(list(before)):
            result = self.load(*key)
            self.assertEqual(result["unloaded"], [])
            self.assertEqual(result["generation"], 6)
            self.assertIs(self.manager.entries[key].model, before[key])
        self.assertEqual(len(self.loaded_calls), 6)

    def test_server_retention_overrides_client_permission_for_count_ram_and_vram(self):
        self.manager.policy = dict(DEFAULT_POLICY)
        self.load("a", "cuda")
        before = self.manager.snapshot()
        for resource in ("count", "ram", "vram"):
            with self.subTest(resource=resource):
                self.manager.policy["max_loaded_models"] = 1 if resource == "count" else 6
                self.manager.memory = lambda device: {"ram": 500 if resource == "ram" else 20000,
                                                      "vram": 500 if resource == "vram" else 20000}
                error = self.error("insufficient_capacity", lambda: self.load("b", "cuda", auto_unload=True))
                self.assertEqual(error.status, 409)
                self.assertIsNone(error.approval)
                self.assertIn("既存モデルを保持", str(error))
                self.assertEqual(self.manager.snapshot()["loaded_models"], before["loaded_models"])
                self.assertEqual(self.manager.generation, before["generation"])
        self.assertEqual(len(self.loaded_calls), 1)

    def test_previous_load_approval_cannot_override_server_retention(self):
        self.manager.policy["max_loaded_models"] = 1
        self.load("a")
        token = self.approval(lambda: self.load("b"))["token"]
        self.manager.policy["allow_auto_unload"] = False
        self.error("insufficient_capacity", lambda: self.load("b", token=token))
        self.assertEqual(list(self.manager.entries), [("a", "cpu")])
        self.assertEqual(len(self.loaded_calls), 1)

    def test_retention_still_allows_explicit_unload_only_with_approval(self):
        self.manager.policy = dict(DEFAULT_POLICY)
        self.load("a")
        approval = self.approval(lambda: self.manager.unload(("a", "cpu")))
        self.assertEqual(list(self.manager.entries), [("a", "cpu")])
        result = self.manager.unload(("a", "cpu"), approval["token"])
        self.assertEqual(result["unloaded"], [{"model": "a", "device": "cpu"}])
        self.assertFalse(self.manager.entries)


if __name__ == "__main__":
    unittest.main()
