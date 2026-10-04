"""稼働サービスへ接続せず、同じ常駐モデルの逐次・一括推論を比較する。"""

import argparse
from datetime import datetime, timezone
import hashlib
import inspect
import json
import math
import os
from pathlib import Path
import statistics
import sys
import time


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="jeff-qwen-0.8b")
    parser.add_argument("--device", choices=["cpu", "cuda"], default="cpu")
    parser.add_argument("--threads", type=int, nargs="+", default=[4, 8])
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.samples <= 20:
        parser.error("samplesは1～20です。")
    if any(not 1 <= count <= 32 for count in args.threads):
        parser.error("threadsはそれぞれ1～32です。")
    return args


def fixtures(model, device):
    from fds.contracts import DecisionRequest
    instructions = [
        "猫は動物ですか？", "猫は植物ですか？", "猫は人間が使う道具ですか？",
        "猫は生き物に分類されますか？", "猫には毛がありますか？",
        "猫は魚と同じ種類の動物ですか？", "猫は哺乳類ですか？",
        "猫は根を張って土から栄養を吸収する植物ですか？",
    ]
    for count in [1, 8]:
        yield f"questions_{count}", DecisionRequest(
            model=model, device=device, state="猫は毛のある哺乳類で、動物です。",
            questions={f"q{i}": {"type": "noul", "instructions": text}
                       for i, text in enumerate(instructions[:count])},
        )


def rows_from(request):
    from fds.contracts import prepare_images
    images = prepare_images(request.images)
    return [{"state": request.state, "question": question.model_dump(exclude_none=True), "images": images}
            for question in request.questions.values()]


def verify_prepared_inputs(torch, resident, request):
    """計測外でpaddingを除いた各入力テンソルと選択肢順の一致を検査する。"""
    rows = rows_from(request)
    per_question_tokens = []
    with torch.inference_mode():
        batch = resident.model.prepare(rows)
        for index, row in enumerate(rows):
            single = resident.model.prepare([row])
            if single.input_tokens > 8192:
                raise ValueError("モデル入力は1質問8192トークン以内にしてください。")
            per_question_tokens.append(single.input_tokens)
            if set(single.inputs) != set(batch.inputs) or single.counts[0] != batch.counts[index]:
                raise RuntimeError("逐次と一括の入力属性または選択肢数が異なります。")
            single_mask = single.inputs["attention_mask"][0].bool()
            batch_mask = batch.inputs["attention_mask"][index].bool()
            for name, single_tensor in single.inputs.items():
                batch_tensor = batch.inputs[name]
                if (single_tensor.ndim >= 2 and batch_tensor.ndim >= 2
                        and single_tensor.shape[:2] == (1, single_mask.numel())
                        and batch_tensor.shape[:2] == (len(rows), batch_mask.numel())):
                    left, right = single_tensor[0][single_mask], batch_tensor[index][batch_mask]
                elif (single_tensor.ndim >= 1 and batch_tensor.ndim >= 1
                      and single_tensor.shape[0] == 1 and batch_tensor.shape[0] == len(rows)):
                    left, right = single_tensor[0], batch_tensor[index]
                else:
                    left, right = single_tensor, batch_tensor
                if not torch.equal(left, right):
                    raise RuntimeError(f"逐次と一括の入力テンソルが異なります: q{index}/{name}")
        if sum(per_question_tokens) != batch.input_tokens:
            raise RuntimeError("逐次と一括の入力トークン合計が異なります。")
    return {"all_unpadded_tensors_equal": True, "input_keys": sorted(batch.inputs),
            "per_question_tokens": dict(zip(request.questions, per_question_tokens)),
            "total_input_tokens": batch.input_tokens,
            "padded_sequence_length": int(batch.inputs["attention_mask"].shape[1])}


def experimental_batched(torch, backend, request):
    from jeff.model import answer
    management = backend.manage("load", request)
    resident = backend.manager.entries[(request.model, management["device"])]
    started = time.perf_counter()
    rows = rows_from(request)
    with torch.inference_mode():
        batch = resident.model.prepare(rows)
        if any(int(mask.sum()) > 8192 for mask in batch.inputs["attention_mask"]):
            raise ValueError("モデル入力は1質問8192トークン以内にしてください。")
        distributions = (resident.model(batch) / resident.model.temperature).softmax(-1).cpu().tolist()
        answers = {}
        for (key, question), values, count in zip(request.questions.items(), distributions, batch.counts, strict=True):
            probabilities = values[:count]
            if any(not math.isfinite(value) for value in probabilities):
                raise RuntimeError("モデルが不正な確率を返しました。")
            answers[key] = answer(question.model_dump(exclude_none=True), probabilities)
    return {"model": request.model, "device": management["device"], "revision": resident.revision,
            "answers": answers, "inference_ms": (time.perf_counter() - started) * 1000,
            "load_ms": management["load_ms"], "usage": {"input_tokens": batch.input_tokens}}


def measure(torch, device, call):
    if device == "cuda":
        torch.cuda.synchronize()
        torch.cuda.reset_peak_memory_stats()
    started = time.perf_counter()
    value = call()
    if device == "cuda":
        torch.cuda.synchronize()
    elapsed = (time.perf_counter() - started) * 1000
    result = {"wall_ms": elapsed, "inference_ms": value["inference_ms"], "load_ms": value["load_ms"],
              "input_tokens": value["usage"]["input_tokens"], "question_order": list(value["answers"]),
              "answers": value["answers"],
              "probabilities": {key: {"false": 1 - answer["noul"], "true": answer["noul"]}
                                for key, answer in value["answers"].items()}}
    if device == "cuda":
        result["cuda_peak_allocated_bytes"] = torch.cuda.max_memory_allocated()
    return result


def difference(left, right):
    if left["question_order"] != right["question_order"] or left["input_tokens"] != right["input_tokens"]:
        raise RuntimeError("逐次と一括で回答順または入力トークン数が異なります。")
    differences = {key: max(abs(left["probabilities"][key][option] - right["probabilities"][key][option])
                            for option in ["false", "true"]) for key in left["question_order"]}
    return {"per_question_max_abs": differences, "max_abs": max(differences.values())}


def summarize(samples, method):
    result = {}
    for metric in ["wall_ms", "inference_ms"]:
        values = [sample[method][metric] for sample in samples]
        result[metric] = {"median": statistics.median(values), "min": min(values), "max": max(values)}
    return result


def main():
    args = arguments()
    # ローカルに固定版が存在しない場合もネットワーク取得へ切り替えない。
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    import torch
    from fds.backend import JeffBackend
    from fds.cli import configuration
    from fds.contracts import ModelOperation

    config = configuration(ROOT)
    config["cpu_threads"] = args.threads[0]
    # 本体がバッチ対応になっても、比較基準は従来の1質問ずつに固定する。
    config["text_batch_size"] = 1
    backend = JeffBackend(ROOT, config)
    # 通常の管理経路を使い、ロード許可・revision・容量検査を省略しない。
    loading = backend.manage("load", ModelOperation(model=args.model, device=args.device))
    resident = backend.manager.entries[(args.model, args.device)]
    parameter = next(resident.model.parameters())
    result = {"started_at_utc": datetime.now(timezone.utc).isoformat(), "model": args.model,
              "revision": resident.revision, "device": args.device, "parameter_device": str(parameter.device),
              "parameter_dtype": str(parameter.dtype), "torch_version": torch.__version__,
              "backend_decide_sha256": hashlib.sha256(inspect.getsource(JeffBackend.decide).encode()).hexdigest(),
              "load_ms": loading["load_ms"], "samples": args.samples, "threads": args.threads,
              "fixture": "synthetic_japanese_cat_v1", "cases": [],
              "notes": ["HTTP・待ち列を含まない同一プロセス内の比較。モデルロード時間は比較から除外。",
                        "各threads・質問数・方式の最初の1回をwarmupとして除外。各sampleはAB/BA交互。",
                        "確率はJeff answerによる正規化後。入力本文・重みは出力しない。",
                        "fds_decideはtext_batch_size=1で逐次処理に固定。一括は実験用。ソースhashで実装を識別する。"]}
    for threads in args.threads:
        torch.set_num_threads(threads)
        backend.config["cpu_threads"] = threads
        for label, request in fixtures(args.model, args.device):
            print(f"計測開始: threads={threads}, {label}", flush=True)
            verification = verify_prepared_inputs(torch, resident, request)
            methods = {"fds_decide": lambda: backend.decide(request),
                       "experimental_batched": lambda: experimental_batched(torch, backend, request)}
            warmup = {name: measure(torch, args.device, call) for name, call in methods.items()}
            samples = []
            for index in range(args.samples):
                order = list(methods) if index % 2 == 0 else list(reversed(methods))
                sample = {"index": index, "order": order}
                for name in order:
                    sample[name] = measure(torch, args.device, methods[name])
                sample["probability_difference"] = difference(sample["fds_decide"], sample["experimental_batched"])
                samples.append(sample)
            summary = {name: summarize(samples, name) for name in methods}
            summary["max_abs_probability_difference"] = max(s["probability_difference"]["max_abs"] for s in samples)
            summary["fds_to_batched_wall_ratio"] = summary["fds_decide"]["wall_ms"]["median"] / summary["experimental_batched"]["wall_ms"]["median"]
            result["cases"].append({"case": label, "requested_threads": threads,
                                    "effective_threads": torch.get_num_threads(),
                                    "interop_threads": torch.get_num_interop_threads(),
                                    "input_verification": verification, "warmup_excluded": warmup,
                                    "warmup_probability_difference": difference(warmup["fds_decide"], warmup["experimental_batched"]),
                                    "summary": summary, "samples": samples})
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({"threads": threads, "case": label, "summary": summary}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"計測を中止しました: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
        raise SystemExit(1) from error
