"""Cloudflare Workers AIの判定。認証値・入力・外部エラー本文は公開しない。"""
import base64
import io
import json
import math
import os
import re
import time

import httpx

from .contracts import prepare_images


CLOUDFLARE_MODELS = {"clef", "clef-flash"}
DEFAULT_CLOUDFLARE = {"account_id_env": "CLOUDFLARE_ACCOUNT_ID",
                      "api_token_env": "CLOUDFLARE_AUTH_TOKEN"}


class CloudflareError(RuntimeError):
    def __init__(self, code, message, status=503):
        super().__init__(message)
        self.code, self.status = code, status

    def response(self):
        return {"error": {"code": self.code, "type": "provider_error", "message": str(self)},
                "detail": {"code": self.code, "message": str(self)}}


def probability(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1


def checked_answers(result, questions):
    """外部応答の既知フィールドだけを検証して返す。"""
    rows = result.get("answers")
    if not isinstance(rows, dict) or set(rows) != set(questions):
        raise ValueError()
    answers = {}
    for key, question in questions.items():
        row = rows[key]
        if not isinstance(row, dict) or row.get("type") != question.type:
            raise ValueError()
        if question.type == "noul":
            if not probability(row.get("noul")):
                raise ValueError()
            answers[key] = {"type": "noul", "noul": row["noul"]}
            continue
        options = list(question.criteria) if question.type == "choice" else [str(i) for i in range(len(question.criteria))]
        probs = row.get("probabilities")
        if (not isinstance(probs, dict) or set(probs) != set(options)
                or not all(probability(p) for p in probs.values())
                or not math.isclose(sum(probs.values()), 1, abs_tol=0.001)
                or not probability(row.get("confidence"))):
            raise ValueError()
        answer = {"type": question.type, "confidence": row["confidence"],
                  "probabilities": {option: probs[option] for option in options}}
        if question.type == "choice":
            selected = row.get("choice")
            if not isinstance(selected, str) or selected not in options:
                raise ValueError()
            answer["choice"] = selected
        else:
            score = row.get("score")
            if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= len(options) - 1:
                raise ValueError()
            answer.update(score=score, legend={str(i): label for i, label in enumerate(question.criteria)})
        answers[key] = answer
    return answers


class CloudflareBackend:
    def __init__(self, config, transport=None):
        self.config = config
        self.settings = DEFAULT_CLOUDFLARE | config.get("cloudflare", {})
        self.transport = transport

    def _credentials(self):
        account = os.environ.get(self.settings["account_id_env"], "").strip()
        token = os.environ.get(self.settings["api_token_env"], "").strip()
        if not re.fullmatch(r"[a-fA-F0-9]{32}", account):
            raise CloudflareError("cloudflare_not_configured", "CloudflareのアカウントID環境変数が未設定または不正です。")
        if not token or len(token) > 4096 or any(not 33 <= ord(c) <= 126 for c in token):
            raise CloudflareError("cloudflare_not_configured", "CloudflareのAPIトークン環境変数が未設定または不正です。")
        return account, token

    def unavailable_reason(self):
        try:
            self._credentials()
        except CloudflareError as error:
            return str(error)
        return None

    def validate_request(self, request):
        if request.device not in ("auto", "cloud"):
            raise CloudflareError("device_unavailable", "Clefはクラウド実行です。deviceにautoまたはcloudを指定してください。", 422)
        if request.approval_token is not None:
            raise CloudflareError("approval_invalid", "クラウド要求にローカル解放用の承認tokenは使用できません。", 409)
        if any(not re.fullmatch(r"[a-zA-Z0-9_.-]{1,100}", key) for key in request.questions):
            raise CloudflareError("cloudflare_input_invalid", "Clefの質問IDは英数字・_・.・-で指定してください。", 422)
        self._credentials()

    def decide(self, request):
        self.validate_request(request)
        account, token = self._credentials()
        model = self.config["models"][request.model]["cloudflare_model"]
        if model not in CLOUDFLARE_MODELS:
            raise CloudflareError("cloudflare_model_invalid", "未対応のCloudflareモデルです。", 422)
        images = prepare_images(request.images)
        payload = {"model": model, "state": request.state,
                   "questions": {k: q.model_dump(exclude_none=True) for k, q in request.questions.items()}}
        encoded, total = [], 0
        for image in images:
            with io.BytesIO() as stream:
                image.save(stream, format="PNG")
                raw = stream.getvalue()
            total += len(raw)
            if len(raw) > 4 * 1024 ** 2 or total > 8 * 1024 ** 2:
                raise CloudflareError("cloudflare_input_invalid", "Clefへ送る正規化画像は1枚4MiB、合計8MiB以内にしてください。", 422)
            encoded.append("data:image/png;base64," + base64.b64encode(raw).decode("ascii"))
        if encoded:
            payload["images"] = encoded
        content = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(content) > 13 * 1024 ** 2:
            raise CloudflareError("cloudflare_input_invalid", "Clefへ送る要求は13MiB以内にしてください。", 422)
        url = f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/@cf/cloudflare/{model}"
        started = time.perf_counter()
        try:
            # リダイレクト・再送・環境のHTTPプロキシは使わず公式宛先へ送る。
            with httpx.Client(timeout=httpx.Timeout(request.timeout_seconds, connect=min(10, request.timeout_seconds)),
                              follow_redirects=False, trust_env=False, transport=self.transport) as client:
                with client.stream("POST", url, content=content,
                                   headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"}) as response:
                    if response.status_code in (401, 403):
                        raise CloudflareError("cloudflare_auth_failed", "Cloudflareの認証またはWorkers AIの権限を確認してください。")
                    if response.status_code == 429:
                        raise CloudflareError("cloudflare_rate_limited", "Cloudflareの利用上限または混雑により処理できません。自動再送はしません。")
                    if not 200 <= response.status_code < 300:
                        raise CloudflareError("cloudflare_request_failed", f"Cloudflareが要求を受理しませんでした（HTTP {response.status_code}）。", 502)
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        if len(data) + len(chunk) > 1024 ** 2:
                            raise CloudflareError("cloudflare_invalid_response", "Cloudflareの応答が上限を超えました。", 502)
                        data.extend(chunk)
        except httpx.TimeoutException:
            raise CloudflareError("cloudflare_timeout", "Cloudflareへの要求が期限を超えました。処理済みの可能性があるため自動再送はしません。", 504) from None
        except httpx.HTTPError:
            raise CloudflareError("cloudflare_connection_failed", "Cloudflareとの通信に失敗しました。自動再送はしません。", 502) from None
        try:
            envelope = json.loads(data)
            if not isinstance(envelope, dict):
                raise ValueError()
            # RESTのresult包絡と、SystemOneの直接応答の両方を扱う。
            if "success" in envelope or "result" in envelope:
                if envelope.get("success") is not True:
                    raise ValueError()
                result = envelope["result"]
            else:
                result = envelope
            if not isinstance(result, dict) or result.get("model") not in (model, "@cf/cloudflare/" + model):
                raise ValueError()
            answers = checked_answers(result, request.questions)
            usage = result["usage"]
            if not isinstance(usage, dict) or any(type(usage.get(k)) is not int or usage[k] < 0 for k in ("input_tokens", "output_tokens")):
                raise ValueError()
        except (ValueError, KeyError, TypeError, OverflowError):
            raise CloudflareError("cloudflare_invalid_response", "Cloudflareの応答が判定仕様と一致しません。", 502) from None
        return {"model": request.model, "revision": None, "device": "cloud", "provider": "cloudflare",
                "upstream_model": "@cf/cloudflare/" + model, "answers": answers,
                "usage": {k: usage[k] for k in ("input_tokens", "output_tokens")},
                "load_ms": 0.0, "inference_ms": (time.perf_counter() - started) * 1000,
                "execution": {"mode": "cloud", "timing_scope": "upstream_round_trip"}}
