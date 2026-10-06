"""ローカル常駐とクラウド判定を明示的なモデル指定で振り分ける。"""
from .backend import JeffBackend
from .cloudflare import CloudflareBackend, CloudflareError


class FdsBackend:
    def __init__(self, root, config, local=None, transport=None):
        self.config = config
        local_config = config | {"models": {k: v for k, v in config["models"].items()
                                           if v.get("backend", "jeff") != "cloudflare"}}
        self.local = local if local is not None else JeffBackend(root, local_config)
        self.cloud = CloudflareBackend(config, transport)

    def is_cloud(self, model):
        return self.config["models"].get(model, {}).get("backend") == "cloudflare"

    @property
    def loaded(self):
        return self.local.loaded

    def status(self):
        state = self.local.status()
        configured = any(self.is_cloud(k) for k in self.config["models"]) and self.cloud.unavailable_reason() is None
        return state | {"ready": state["ready"] or configured, "cloud_configured": configured}

    def capabilities(self):
        result = self.local.capabilities()
        rows = list(result["models"])
        reason = self.cloud.unavailable_reason()
        for ident, entry in self.config["models"].items():
            if not self.is_cloud(ident):
                continue
            rows.append({"id": ident, "object": "model", "name": entry["name"], "revision": None,
                         "backend": "cloudflare", "execution_location": "cloud", "installed": False,
                         "available": reason is None, "unavailable_reason": reason,
                         "modalities": ["text", "image"] if entry.get("images") else ["text"],
                         "devices": ["auto", "cloud"] if reason is None else [],
                         "loadable": False, "load_allowed": False, "unload_allowed": False,
                         "management_supported": False, "loaded_devices": [],
                         "status": {"value": "configured" if reason is None else "unavailable"}})
        return result | {"models": rows, "data": rows, "capabilities_version": 3}

    def validate_request(self, request):
        if self.is_cloud(request.model):
            self.cloud.validate_request(request)
        else:
            self.local.validate_request(request)

    def warmup(self):
        if self.is_cloud(self.config["default_model"]):
            self.cloud._credentials()  # 起動時は認証設定だけ確認し、課金対象の要求を送らない。
        else:
            self.local.warmup()

    def manage(self, action, request):
        if self.is_cloud(request.model):
            raise CloudflareError("cloud_management_unsupported", "クラウドモデルはダウンロード・ロード・アンロード不要です。判定APIを使用してください。", 422)
        return self.local.manage(action, request)

    def decide(self, request):
        if self.is_cloud(request.model):
            return self.cloud.decide(request)
        return self.local.decide(request)
