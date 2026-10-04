"""Jeffモデルの実装。常駐管理は推論と同一ワーカーだけが変更する。"""
import gc
import json
import math
import os
import time
from pathlib import Path

from .contracts import prepare_images
from .management import ManagementError, Resident, ResidentModels


TEXT_BATCH_TOKEN_LIMIT = 1024


def free_memory_mb(device):
    """OS/CUDAの現在の空き。取得不能を架空の空き容量に置き換えない。"""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD),
                        ("total_phys", ctypes.c_ulonglong), ("avail_phys", ctypes.c_ulonglong),
                        ("total_page", ctypes.c_ulonglong), ("avail_page", ctypes.c_ulonglong),
                        ("total_virtual", ctypes.c_ulonglong), ("avail_virtual", ctypes.c_ulonglong),
                        ("avail_extended", ctypes.c_ulonglong)]
        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            raise OSError("空きRAMを取得できません。")
        ram = status.avail_phys / 1024 ** 2
    else:
        ram = os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 1024 ** 2
    vram = 0
    if device == "cuda":
        import torch
        vram = torch.cuda.mem_get_info()[0] / 1024 ** 2
    return {"ram": ram, "vram": vram}


class JeffBackend:
    def __init__(self, root: Path, config: dict):
        self.root, self.config = root, config
        self.manager = ResidentModels(config, self._create_model, self._collect, free_memory_mb)

    @property
    def loaded(self):
        return self.manager.snapshot()["loaded"]

    @property
    def model(self):
        resident = self.manager.entries.get(self.manager.current)
        return resident.model if resident else None

    @property
    def limit(self):
        resident = self.manager.entries.get(self.manager.current)
        return resident.limit if resident else None

    @limit.setter
    def limit(self, value):
        self.manager.entries[self.manager.current].limit = value

    def status(self):
        return self.manager.snapshot()

    def _installed(self, entry):
        folder = self.root / entry["checkpoint"]
        try:
            marker = folder / ".fds-revision"
            return ((folder / "decision_config.json").is_file() and marker.is_file()
                    and marker.read_text(encoding="utf-8").strip() == entry["revision"]
                    and any(folder.glob("*.safetensors")))
        except OSError:
            return False

    def capabilities(self):
        import torch
        cuda = torch.cuda.is_available()
        devices = ["cpu"] + (["cuda"] if cuda else [])
        automatic = self.config.get("default_device", "auto")
        if automatic != "cuda" or cuda:
            devices.insert(0, "auto")
        state = self.manager.snapshot()
        policy = state["model_management"]
        rows = []
        for ident, entry in self.config["models"].items():
            installed = self._installed(entry)
            supported = entry.get("backend", "jeff") == "jeff"
            available = bool(installed and supported)
            loaded_devices = [m["device"] for m in state["loaded_models"] if m["model"] == ident]
            operation = state["operation"]
            status = operation["action"] if operation and operation["model"] == ident else (
                "ready" if loaded_devices else "unloaded" if available else "unavailable")
            rows.append({"id": ident, "object": "model", "name": entry["name"], "revision": entry["revision"],
                         "modalities": ["text", "image"] if entry.get("images") else ["text"],
                         "backend": entry.get("backend", "jeff"), "installed": bool(installed), "available": available,
                         "unavailable_reason": None if available else "モデル未導入または固定revision不一致" if not installed else "未対応バックエンド",
                         "devices": list(devices) if available else [],
                         "loadable": available and policy["allow_load"],
                         "load_allowed": policy["allow_load"], "unload_allowed": policy["allow_unload"],
                         "loaded_devices": loaded_devices, "status": {"value": status}})
        return {"object": "list", "data": rows, "models": rows, "devices": devices,
                "default_device": automatic, "capabilities_version": 2,
                "loaded_models": state["loaded_models"], "model_management": policy,
                "generation": state["generation"], "operation": state["operation"]}

    def _target(self, name, requested_device, installed=True):
        import torch
        entry = self.config["models"].get(name)
        if entry is None:
            raise ManagementError("model_unknown", "未登録のモデルです。", 422)
        if entry.get("backend", "jeff") != "jeff":
            raise ManagementError("backend_unsupported", "未対応のバックエンドです。", 422)
        device = self.config.get("default_device", "auto") if requested_device == "auto" else requested_device
        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else "cpu"
        if device not in ("cpu", "cuda") or (device == "cuda" and not torch.cuda.is_available()):
            raise ManagementError("device_unavailable", "指定デバイスはサーバーで利用できません。CUDA利用可否を確認してください。", 422)
        if installed and not self._installed(entry):
            raise ManagementError("model_not_installed", "モデルが未導入または固定revisionと一致しません。", 422)
        return (name, device)

    def validate_request(self, request):
        self._target(request.model, request.device)

    def _collect(self):
        import torch
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    def _create_model(self, key, memory):
        import torch
        from jeff.models import load_decision_model
        name, device = key
        entry = self.config["models"][name]
        folder = self.root / entry["checkpoint"]
        metadata = json.loads((folder / "decision_config.json").read_text(encoding="utf-8"))
        limit = metadata["max_options"]
        if type(limit) is not int or limit < 2:
            raise ValueError("モデルの選択肢上限が不正です。")
        torch.set_num_threads(self.config.get("cpu_threads", 4))
        model = None
        try:
            model = load_decision_model(checkpoint=str(folder), device=device,
                                        cpu_threads=self.config.get("cpu_threads", 4))
            model.eval()
            return Resident(model, limit, entry["revision"], memory)
        except Exception:
            model = None
            self._collect()
            raise

    def manage(self, action, request, startup=False):
        key = self._target(request.model, request.device, installed=action == "load")
        if action == "unload":
            return self.manager.unload(key, request.approval_token)
        if action != "load":
            raise ValueError("未対応の管理操作です。")
        estimate = self.config["models"][request.model].get("memory_mb", {}).get(key[1])
        return self.manager.load(key, estimate, request.auto_unload, request.approval_token, startup)

    def _load(self, name, requested_device):
        from .contracts import ModelOperation
        result = self.manage("load", ModelOperation(model=name, device=requested_device))
        return result["device"], result["load_ms"]

    def warmup(self):
        from .contracts import DecisionRequest
        body = DecisionRequest(state="準備確認", model=self.config["default_model"],
                               questions={"ready": {"type": "noul", "instructions": "これは準備確認ですか？"}})
        self.manage("load", body, startup=True)
        self.decide(body)

    def decide(self, request):
        import torch
        from jeff.model import answer
        entry = self.config["models"][request.model]
        if request.images and not entry.get("images", False):
            raise ValueError("このモデルは画像入力に対応していません。")
        images = prepare_images(request.images)
        management = self.manage("load", request)
        device, load_ms = management["device"], management["load_ms"]
        resident = self.manager.entries[(request.model, device)]
        started = time.perf_counter()
        answers, tokens = {}, 0
        questions = [(key, question.model_dump(exclude_none=True))
                     for key, question in request.questions.items()]
        for _, question in questions:
            if question["type"] == "choice" and len(question["criteria"]) > resident.limit:
                raise ValueError("選択肢がモデルの上限を超えました。")
        batch_size = 1 if images else self.config.get("text_batch_size", 8)
        batch_sizes = []
        pending = [questions[start:start + batch_size] for start in range(0, len(questions), batch_size)]
        pending.reverse()
        with torch.inference_mode():
            while pending:
                items = pending.pop()
                rows = [{"state": request.state, "question": value, "images": images} for _, value in items]
                # Jeff の各質問8192トークン制限と、プロンプト生成をそのまま使う。
                batch = resident.model.prepare(rows, max_length=8192)
                if len(items) > 1 and batch.inputs["input_ids"].numel() > TEXT_BATCH_TOKEN_LIMIT:
                    # paddingを含む入力量が大きいときは、親のGPU入力を解放して再準備する。
                    del batch
                    midpoint = len(items) // 2
                    pending.append(items[midpoint:])
                    pending.append(items[:midpoint])
                    continue
                if len(items) == 1 and batch.input_tokens > 8192:
                    raise ValueError("モデル入力は1質問8192トークン以内にしてください。")
                distributions = (resident.model(batch) / resident.model.temperature).softmax(-1).cpu().tolist()
                batch_sizes.append(len(items))
                for (key, value), distribution, count in zip(items, distributions, batch.counts, strict=True):
                    probabilities = distribution[:count]
                    if any(not math.isfinite(p) for p in probabilities):
                        raise RuntimeError("モデルが不正な確率を返しました。")
                    answers[key] = answer(value, probabilities)
                tokens += batch.input_tokens
                del batch
        return {"model": request.model, "revision": entry["revision"], "device": device,
                "answers": answers, "load_ms": load_ms, "inference_ms": (time.perf_counter()-started)*1000,
                "usage": {"input_tokens": tokens, "output_tokens": 0}, "provider": "fds",
                "execution": {"batch_sizes": batch_sizes, "cpu_threads": torch.get_num_threads(),
                              "text_batch_token_limit": TEXT_BATCH_TOKEN_LIMIT},
                "management": management}
