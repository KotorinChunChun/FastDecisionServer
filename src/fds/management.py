"""単一ワーカー所有の常駐管理。読取りだけは短いロックでスナップショット化する。"""
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
import math
import secrets
import threading
import time


DEFAULT_POLICY = {
    "allow_load": True, "allow_unload": True, "max_loaded_models": 3,
    "ram_reserve_mb": 2048, "vram_reserve_mb": 1024, "approval_ttl_seconds": 120,
}


class ManagementError(ValueError):
    def __init__(self, code, message, status=409, approval=None):
        super().__init__(message)
        self.code, self.status, self.approval = code, status, approval

    def response(self):
        detail = {"code": self.code, "message": str(self)}
        if self.approval is not None:
            detail["approval"] = self.approval
        return {"error": {"code": self.code, "type": "model_management_error", "message": str(self)},
                "detail": detail}


@dataclass
class Resident:
    model: object
    limit: int
    revision: str
    memory: dict


class ResidentModels:
    """変更メソッドは必ず推論と同じワーカーから呼び出す。"""
    def __init__(self, config, loader, disposer, memory):
        self.policy = DEFAULT_POLICY | config.get("model_management", {})
        self.entries = OrderedDict()
        self.current = None
        self.generation = 0
        self.operation = None
        self.last_error = None
        self._lock = threading.RLock()
        self._challenges = OrderedDict()
        self.loader, self.disposer, self.memory = loader, disposer, memory

    def snapshot(self):
        with self._lock:
            return {"loaded_models": [{"model": key[0], "device": key[1], "revision": value.revision,
                                       "status": "ready"} for key, value in self.entries.items()],
                    "generation": self.generation, "model_management": dict(self.policy),
                    "operation": dict(self.operation) if self.operation else None,
                    "loaded": self.current, "ready": bool(self.entries), "error": self.last_error}

    def _set_operation(self, action=None, key=None):
        with self._lock:
            self.operation = {"action": action, "model": key[0], "device": key[1]} if action else None

    def _available(self, device):
        try:
            available = self.memory(device)
            required = ("ram", "vram") if device == "cuda" else ("ram",)
            if not isinstance(available, dict) or any(
                type(available.get(k)) not in (int, float) or not math.isfinite(available[k]) or available[k] < 0
                for k in required
            ):
                raise ValueError("空きメモリが不明です。")
            return {"ram": float(available["ram"]), "vram": float(available.get("vram", 0))}
        except ManagementError:
            raise
        except Exception:
            raise ManagementError("memory_unavailable", "空きメモリを取得できないためロードできません。", 503) from None

    def _plan(self, key, estimate, auto_unload):
        if not isinstance(estimate, dict) or any(
            type(estimate.get(k)) not in (int, float) or not math.isfinite(estimate[k]) or estimate[k] < 0
            for k in ("ram", "vram")
        ) or estimate["ram"] <= 0 or (key[1] == "cuda" and estimate["vram"] <= 0):
            raise ManagementError("memory_estimate_missing", "このモデルのメモリ見積が未設定です。", 503)
        free = self._available(key[1])
        reserves = {"ram": self.policy["ram_reserve_mb"],
                    "vram": self.policy["vram_reserve_mb"] if key[1] == "cuda" else 0}
        resources = ("ram", "vram") if key[1] == "cuda" else ("ram",)
        victims = []
        def enough():
            return (len(self.entries) - len(victims) < self.policy["max_loaded_models"]
                    and all(free[r] >= estimate[r] + reserves[r] for r in resources))
        if enough():
            return victims
        if not auto_unload:
            raise ManagementError("insufficient_capacity", "空き容量または常駐上限が不足しています。自動アンロードは無効です。")
        if not self.policy["allow_unload"]:
            raise ManagementError("unload_not_allowed", "サーバーがアンロードを許可していないため追加ロードできません。", 403)
        for candidate, resident in self.entries.items():
            victims.append(candidate)
            for resource in resources:
                free[resource] += resident.memory[resource]
            if enough():
                return victims
        raise ManagementError("insufficient_capacity", "常駐モデルを解放しても必要な空き容量を確保できません。")

    def _challenge(self, action, key, victims, reason):
        now = time.monotonic()
        for token in list(self._challenges):
            if self._challenges[token]["deadline"] <= now:
                self._challenges.pop(token, None)
        while len(self._challenges) >= 128:
            self._challenges.popitem(last=False)
        token = secrets.token_urlsafe(32)
        expires = datetime.fromtimestamp(time.time() + self.policy["approval_ttl_seconds"], timezone.utc).isoformat()
        approval = {"token": token, "expires_at": expires, "action": action, "model": key[0], "device": key[1],
                    "unload": [{"model": name, "device": device} for name, device in victims],
                    "reason": reason, "generation": self.generation}
        self._challenges[token] = {"deadline": now + self.policy["approval_ttl_seconds"],
                                    "action": action, "key": key, "victims": tuple(victims),
                                    "generation": self.generation}
        return approval

    def _authorize(self, action, key, victims, token):
        reason = "指定した常駐モデルを解放します。" if action == "unload" else "空き容量または常駐上限のため既存モデルの解放が必要です。"
        if token is not None:
            record = self._challenges.pop(token, None)
            if record is None or record["action"] != action or record["key"] != key:
                raise ManagementError("approval_invalid", "承認が不明、使用済み、または操作対象と一致しません。")
            if (record["deadline"] <= time.monotonic() or record["generation"] != self.generation
                    or record["victims"] != tuple(victims)):
                if victims:
                    raise ManagementError("approval_required", "承認期限または常駐状態が変わりました。再確認してください。",
                                          approval=self._challenge(action, key, victims, reason))
                raise ManagementError("approval_invalid", "承認時から常駐状態が変わりました。再申請してください。")
            return
        if victims:
            raise ManagementError("approval_required", reason,
                                  approval=self._challenge(action, key, victims, reason))

    def _evict(self, victims):
        for key in victims:
            with self._lock:
                resident = self.entries.pop(key)
                self.generation += 1
                if self.current == key:
                    self.current = next(reversed(self.entries), None)
            # モデル参照を先に捨ててからGC/CUDAキャッシュを解放する。
            resident.model = None
            del resident
        if victims:
            self.disposer()

    def _result(self, key, started, victims):
        state = self.snapshot()
        return {"success": True, "model": key[0], "device": key[1],
                "load_ms": (time.perf_counter() - started) * 1000,
                "unloaded": [{"model": name, "device": device} for name, device in victims],
                "loaded_models": state["loaded_models"], "generation": state["generation"]}

    def load(self, key, estimate, auto_unload=True, token=None, startup=False):
        started = time.perf_counter()
        if key in self.entries:
            self._authorize("load", key, [], token)
            with self._lock:
                self.entries.move_to_end(key)
                self.current = key
                self.last_error = None
            return self._result(key, started, [])
        if not startup and not self.policy["allow_load"]:
            raise ManagementError("load_not_allowed", "サーバーはモデルのロードを許可していません。", 403)
        victims = self._plan(key, estimate, auto_unload)
        self._authorize("load", key, victims, token)
        self._set_operation("loading", key)
        try:
            self._evict(victims)
            # 見積だけでロードせず、実際に解放した後の空きを再確認する。
            if victims:
                self._plan(key, estimate, False)
            resident = self.loader(key, dict(estimate))
            with self._lock:
                self.entries[key] = resident
                self.current = key
                self.generation += 1
                self.last_error = None
            return self._result(key, started, victims)
        except ManagementError as error:
            with self._lock:
                self.last_error = str(error)
            raise
        except Exception:
            self.disposer()
            error = ManagementError("model_load_failed", "モデルのロードに失敗しました。空きメモリと導入ファイルを確認してください。", 503)
            with self._lock:
                self.last_error = str(error)
            raise error from None
        finally:
            self._set_operation()

    def unload(self, key, token=None):
        started = time.perf_counter()
        victims = [key] if key in self.entries else []
        if victims and not self.policy["allow_unload"]:
            raise ManagementError("unload_not_allowed", "サーバーはモデルのアンロードを許可していません。", 403)
        self._authorize("unload", key, victims, token)
        self._set_operation("unloading", key)
        try:
            self._evict(victims)
            with self._lock:
                self.last_error = None
            return self._result(key, started, victims)
        finally:
            self._set_operation()
