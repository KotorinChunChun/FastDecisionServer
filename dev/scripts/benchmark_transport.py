"""同じJeff・デバイス・入力で、直接一括推論とFDSのHTTP応答を比較する。"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import statistics
import time

from benchmark_execution import ROOT, fixtures, experimental_batched


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:18767")
    parser.add_argument("--model", default="jeff-qwen-0.8b")
    parser.add_argument("--device", choices=["cpu", "cuda"], default="cuda")
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.samples <= 100:
        parser.error("samplesは1～100です。")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    import httpx
    import torch
    from fds.backend import JeffBackend
    from fds.cli import configuration
    from fds.contracts import ModelOperation

    config = configuration(ROOT)
    backend = JeffBackend(ROOT, config)
    backend.manage("load", ModelOperation(model=args.model, device=args.device))
    report = {"at_utc": datetime.now(timezone.utc).isoformat(), "model": args.model,
              "device": args.device, "samples": args.samples, "cases": [],
              "boundary": "同じ入力の直接一括推論とHTTP。初回ロードと各方式1回の予備を除外。AB/BA交互。"}
    with httpx.Client(base_url=args.url, timeout=125, trust_env=False) as client:
        health = client.get("/health").raise_for_status().json()
        if (health.get("service") != "FastDecisionServer" or not health.get("ready")
                or health.get("active") or health.get("queued")):
            raise RuntimeError("専用FDSの準備未完了、または別の処理中です。")
        if (args.model, args.device) not in {(m["model"], m["device"]) for m in health["loaded_models"]}:
            raise RuntimeError("比較モデルは先に専用FDSへ常駐させてください。")
        if health.get("cpu_threads") != config["cpu_threads"]:
            raise RuntimeError("CPUスレッド設定が比較先と異なります。")
        report["health_before"] = health
        for label, request in fixtures(args.model, args.device):
            request.auto_unload = False

            def direct():
                return experimental_batched(torch, backend, request)

            def remote():
                return client.post("/v1/decisions", json=request.model_dump(exclude_none=True)).raise_for_status().json()

            def measure(call):
                start = time.perf_counter()
                result = call()
                return {"wall_ms": (time.perf_counter() - start) * 1000, "result": result}

            methods = {"direct": direct, "http": remote}
            warmup = {name: measure(call) for name, call in methods.items()}
            rows = []
            for index in range(args.samples):
                order = list(methods) if index % 2 == 0 else list(reversed(methods))
                row = {name: measure(methods[name]) for name in order}
                left, right = row["direct"]["result"], row["http"]["result"]
                if any(left[key] != right[key] for key in ["model", "device", "revision", "answers"]):
                    raise RuntimeError("直接一括推論とHTTPの実行条件・回答が一致しません。")
                if left["usage"]["input_tokens"] != right["usage"]["input_tokens"]:
                    raise RuntimeError("入力トークン数が一致しません。")
                rows.append(row)
            summary = {name: {"median_ms": statistics.median(row[name]["wall_ms"] for row in rows),
                              "min_ms": min(row[name]["wall_ms"] for row in rows),
                              "max_ms": max(row[name]["wall_ms"] for row in rows)} for name in methods}
            summary["http_over_direct"] = summary["http"]["median_ms"] / summary["direct"]["median_ms"]
            report["cases"].append({"case": label, "warmup_excluded": warmup, "summary": summary, "rows": rows})
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"case": label, "summary": summary}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
