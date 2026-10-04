"""fds の起動・モデル導入・状態・管理停止。"""
import argparse
import json
import math
import os
from pathlib import Path
import re
import secrets
import socket
import urllib.error
import urllib.request


def configuration(root):
    value = json.loads((root / "config.json").read_text(encoding="utf-8-sig"))
    local = root / "config.local.json"
    if local.exists():
        value.update(json.loads(local.read_text(encoding="utf-8-sig")))
    if value["host"] != "127.0.0.1":
        raise ValueError("初版の待受は127.0.0.1のみです。")
    if value["default_device"] not in ("auto", "cpu", "cuda"):
        raise ValueError("deviceが不正です。")
    for key, lower, upper in [("cpu_threads", 1, 32), ("queue_size", 1, 256), ("port", 1, 65535)]:
        if type(value[key]) is not int or not lower <= value[key] <= upper:
            raise ValueError(f"{key}は{lower}～{upper}の整数を指定してください。")
    if value["default_model"] not in value["models"]:
        raise ValueError("既定モデルが未登録です。")
    for name, entry in value["models"].items():
        if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,100}", name) or not re.fullmatch(r"[a-fA-F0-9]{40}", entry["revision"]):
            raise ValueError("モデルIDまたは固定revisionが不正です。")
        folder = (root / entry["checkpoint"]).resolve()
        if not folder.is_relative_to((root / "models").resolve()):
            raise ValueError("checkpointはプロジェクト内のmodels配下に配置してください。")

    from .management import DEFAULT_POLICY
    policy = value.get("model_management", {})
    if not isinstance(policy, dict) or set(policy) - set(DEFAULT_POLICY):
        raise ValueError("model_managementの設定が不正です。")
    policy = DEFAULT_POLICY | policy
    for key in ("allow_load", "allow_unload"):
        if type(policy[key]) is not bool:
            raise ValueError(f"{key}は真偽値で指定してください。")
    for key, lower, upper in [("max_loaded_models", 1, 32), ("ram_reserve_mb", 0, 1048576),
                               ("vram_reserve_mb", 0, 1048576), ("approval_ttl_seconds", 10, 600)]:
        if type(policy[key]) is not int or not lower <= policy[key] <= upper:
            raise ValueError(f"{key}は{lower}～{upper}の整数を指定してください。")
    value["model_management"] = policy
    for entry in value["models"].values():
        estimates = entry.get("memory_mb", {})
        if not isinstance(estimates, dict) or set(estimates) - {"cpu", "cuda"}:
            raise ValueError("memory_mbのデバイス指定が不正です。")
        for device, estimate in estimates.items():
            if not isinstance(estimate, dict) or set(estimate) != {"ram", "vram"}:
                raise ValueError("memory_mbにはramとvramを指定してください。")
            if any(type(n) not in (int, float) or not math.isfinite(n) or not 0 <= n <= 1048576 for n in estimate.values()):
                raise ValueError("メモリ見積は有限の非負MB値で指定してください。")
            if estimate["ram"] <= 0 or (device == "cuda" and estimate["vram"] <= 0) or (device == "cpu" and estimate["vram"] != 0):
                raise ValueError("モデルのRAM/VRAM見積が不正です。")

    return value


def main():
    parser = argparse.ArgumentParser(prog="fds", description="FastDecisionServer（共用判定サーバー）")
    parser.add_argument("command", choices=["serve", "status", "stop", "download"])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--model")
    parser.add_argument("--device", choices=["auto", "cpu", "cuda"])
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    root = args.root.resolve()
    config = configuration(root)
    if args.device:
        config["default_device"] = args.device
    if args.port is not None:
        if not 1 <= args.port <= 65535:
            parser.error("portは1～65535です。")
        config["port"] = args.port
    if args.model:
        if args.model not in config["models"]:
            parser.error("未登録のモデルです。")
        config["default_model"] = args.model
    address = f"http://127.0.0.1:{config['port']}"
    if args.command == "download":
        from huggingface_hub import snapshot_download
        entry = config["models"][config["default_model"]]
        folder = root / entry["checkpoint"]
        snapshot_download(entry["repo"], revision=entry["revision"], local_dir=str(folder))
        (folder / ".fds-revision").write_text(entry["revision"], encoding="utf-8")
    elif args.command == "status":
        with urllib.request.urlopen(address + "/health", timeout=5) as response:
            print(response.read().decode())
    elif args.command == "stop":
        token = (root / "runtime" / f"control-{config['port']}.token").read_text(encoding="utf-8")
        request = urllib.request.Request(address + "/admin/shutdown", data=b"", method="POST", headers={"Authorization": "Bearer " + token})
        with urllib.request.urlopen(request, timeout=5) as response:
            print(response.read().decode())
    else:
        import uvicorn
        from .backend import JeffBackend
        from .server import create_app
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
        # ポートを確保してから管理tokenを書く。二重起動で稼働中tokenを壊さない。
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            listener.bind((config["host"], config["port"]))
        except OSError:
            listener.close()
            raise RuntimeError(f"ポート{config['port']}は利用できません。fds statusで確認してください。") from None
        runtime = root / "runtime"
        runtime.mkdir(exist_ok=True)
        token = secrets.token_urlsafe(32)
        app = create_app(config, JeffBackend(root, config), token, lambda: setattr(server, "should_exit", True))
        server = uvicorn.Server(uvicorn.Config(app, host=config["host"], port=config["port"], access_log=False))
        token_file = runtime / f"control-{config['port']}.token"
        try:
            token_file.write_text(token, encoding="utf-8")
            server.run(sockets=[listener])
        finally:
            listener.close()
            if token_file.exists() and token_file.read_text(encoding="utf-8") == token:
                token_file.unlink()


if __name__ == "__main__":
    main()