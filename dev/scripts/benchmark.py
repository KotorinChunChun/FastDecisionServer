"""実モデル用。入力は固定短文とプログラムで生成した図形画像のみ。"""
import argparse
import base64
import io
import json
import math
from pathlib import Path
import statistics
import time

import httpx
from PIL import Image, ImageDraw


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", choices=["auto", "cpu", "cuda"], required=True)
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--url", default="http://127.0.0.1:8767")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.samples <= 100:
        parser.error("samplesは1～100です。")
    image = Image.new("RGB", (256, 256), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((48, 48, 208, 208), fill="red")
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    image_uri = "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode()
    text = {"state": "猫は動物です。", "questions": {
        "animal": {"type": "noul", "instructions": "猫は動物ですか？"},
        "category": {"type": "choice", "instructions": "猫の分類を選んでください。", "criteria": {"animal": "動物", "tool": "道具"}},
        "match": {"type": "score", "instructions": "猫が動物であることにどれくらい当てはまりますか？", "criteria": ["当てはまらない", "当てはまる"]}}}
    picture = {"state": "画像の色を確認してください。", "questions": {"red": {"type": "noul", "instructions": "画像には赤い四角がありますか？"}}, "images": [image_uri]}
    result = {"device": args.device, "model": "jeff-qwen-2b", "samples": args.samples, "cases": {}, "source": "合成した256x256の赤い四角と固定短文のみ"}
    with httpx.Client(timeout=330) as client:
        for label, content in [("text", text), ("image", picture)]:
            body = content | {"model": "jeff-qwen-2b", "device": args.device, "timeout_seconds": 300}
            def call():
                started = time.perf_counter()
                response = client.post(args.url + "/v1/decisions", json=body)
                response.raise_for_status()
                value = response.json()
                value["client_ms"] = (time.perf_counter() - started) * 1000
                if value["model"] != body["model"] or (args.device != "auto" and value["device"] != args.device):
                    raise RuntimeError("要求と応答のモデル/deviceが異なります。")
                return value
            cold = call()
            rows = [call() for _ in range(args.samples)]
            metrics = {}
            for metric in ["client_ms", "total_ms", "queue_ms", "load_ms", "inference_ms"]:
                values = [row[metric] for row in rows]
                metrics[metric] = {"mean": statistics.mean(values), "median": statistics.median(values),
                                   "min": min(values), "max": max(values),
                                   "p95": sorted(values)[math.ceil(len(values) * .95) - 1] if len(values) >= 20 else None}
            result["cases"][label] = {"warmup_excluded": cold, "summary": metrics, "results": rows}
            print(json.dumps({"case": label, "device": args.device, "n": len(rows), "summary": metrics}, ensure_ascii=False), flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()