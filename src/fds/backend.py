"""モデル固有部分。新しいバックエンドは同じdecide契約で登録する。"""
import gc
import json
import math
import time
from pathlib import Path

from .contracts import prepare_images


class JeffBackend:
    def __init__(self, root: Path, config: dict):
        self.root, self.config = root, config
        self.model = None
        self.loaded = None

    def capabilities(self):
        import torch
        cuda = torch.cuda.is_available()
        server_devices = ['cpu'] + (['cuda'] if cuda else [])
        automatic = self.config.get('default_device','auto')
        auto_supported = automatic != 'cuda' or cuda
        rows=[]
        for ident,entry in self.config['models'].items():
            folder=self.root / entry['checkpoint']
            try:
                marker=folder / '.fds-revision'
                installed=(folder / 'decision_config.json').is_file() and marker.is_file() and marker.read_text(encoding='utf-8').strip()==entry['revision'] and any(folder.glob('*.safetensors'))
            except OSError:
                installed=False
            supported=entry.get('backend','jeff')=='jeff'
            available=bool(installed and supported)
            rows.append({'id':ident,'name':entry['name'],'revision':entry['revision'],
                         'modalities':['text','image'] if entry.get('images') else ['text'],
                         'backend':entry.get('backend','jeff'),'installed':bool(installed),'available':available,
                         'unavailable_reason':None if available else 'モデル未導入または固定版不一致' if not installed else '未対応バックエンド',
                         'devices':(['auto'] if auto_supported else [])+server_devices if available else []})
        return {'models':rows,'devices':(['auto'] if auto_supported else [])+server_devices,
                'default_device':automatic,'capabilities_version':1}

    def validate_request(self, request):
        capabilities = self.capabilities()
        model = next((m for m in capabilities['models'] if m['id'] == request.model), None)
        if model is None or not model['available']:
            raise ValueError(model['unavailable_reason'] if model else '未登録のモデルです。')
        if request.device not in model['devices']:
            raise ValueError('指定デバイスはサーバーで利用できません。対応デバイス: ' + ', '.join(model['devices']))

    def _load(self, name, requested_device):
        import torch
        from jeff.models import load_decision_model
        entry = self.config["models"][name]
        if entry.get("backend") != "jeff":
            raise ValueError("未対応のバックエンドです。")
        device = self.config["default_device"] if requested_device == "auto" else requested_device
        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else "cpu"
        if device == "cuda" and not torch.cuda.is_available():
            raise RuntimeError("CUDAを利用できません。cpuを選択してください。")
        started = time.perf_counter()
        if self.loaded != (name, device):
            folder = self.root / entry["checkpoint"]
            metadata = folder / "decision_config.json"
            if not metadata.is_file():
                raise RuntimeError(f"モデルが未導入です: {name}。fds downloadで導入してください。")
            revision_file = folder / ".fds-revision"
            if not revision_file.is_file() or revision_file.read_text(encoding="utf-8").strip() != entry["revision"]:
                raise RuntimeError(f"モデルの固定revisionを確認できません: {name}。fds downloadで導入してください。")
            self.model = None
            self.loaded = None
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            torch.set_num_threads(self.config.get("cpu_threads", 4))
            self.limit = json.loads(metadata.read_text(encoding="utf-8"))["max_options"]
            self.model = load_decision_model(checkpoint=str(folder), device=device, cpu_threads=self.config.get("cpu_threads", 4))
            self.model.eval()
            self.loaded = (name, device)
        return device, (time.perf_counter() - started) * 1000

    def warmup(self):
        from .contracts import DecisionRequest
        self.decide(DecisionRequest(state="準備確認", model=self.config["default_model"],
                                   questions={"ready":{"type":"noul", "instructions":"これは準備確認ですか？"}}))

    def decide(self, request):
        import torch
        from jeff.model import answer
        entry = self.config["models"][request.model]
        if request.images and not entry.get("images", False):
            raise ValueError("このモデルは画像入力に対応していません。")
        images = prepare_images(request.images)
        device, load_ms = self._load(request.model, request.device)
        started = time.perf_counter()
        answers, tokens = {}, 0
        with torch.inference_mode():
            for key, question in request.questions.items():
                value = question.model_dump(exclude_none=True)
                if question.type == "choice" and len(question.criteria) > self.limit:
                    raise ValueError("選択肢がモデルの上限を超えました。")
                # 質問ごとの処理で一時GPUメモリを抑制する。モデルは全質問で同一。
                batch = self.model.prepare([{"state":request.state, "question":value, "images":images}])
                if batch.input_tokens > 8192:
                    raise ValueError("モデル入力は1質問8192トークン以内にしてください。")
                probabilities = (self.model(batch) / self.model.temperature).softmax(-1).cpu().tolist()[0][:batch.counts[0]]
                if any(not math.isfinite(p) for p in probabilities):
                    raise RuntimeError("モデルが不正な確率を返しました。")
                answers[key] = answer(value, probabilities)
                tokens += batch.input_tokens
        return {"model":request.model, "revision":entry["revision"], "device":device,
                "answers":answers, "load_ms":load_ms, "inference_ms":(time.perf_counter()-started)*1000,
                "usage":{"input_tokens":tokens, "output_tokens":0}, "provider":"fds"}
