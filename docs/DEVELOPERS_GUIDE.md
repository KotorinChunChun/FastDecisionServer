# 開発者ガイド

## 環境

Windows / Python 3.12。プロジェクト専用 `.venv` を使用する。依存導入は `dev/scripts/setup.ps1`。CPU専用版はCUDAやNVIDIA GPUを必要としない。GPU版はCUDA 13.0に対応するドライバーを必要とする。venv・モデル・runtimeはGit対象外。

```powershell
cd C:\develop\FastDecisionServer
pwsh -NoProfile -File .\dev\scripts\setup.ps1 -Device cpu
.\.venv\Scripts\fds.exe download --model jeff-qwen-2b
.\.venv\Scripts\fds.exe serve --device cpu
```

`fds serve` はフォアグラウンドで起動する。`dev/scripts/fds.ps1 -Command start` はウィンドウを出さず起動し、health到達を確認する。起動時にモデルを準備し、初回要求との順序を保証する。ポートを確保した後に管理トークンを書くため、二重起動で既存プロセスの管理トークンを上書きしない。

設定は `config.json`、ローカル上書きは Git 対象外の `config.local.json`。上書きはトップレベル単位（modelsは辞書全体）で適用する。`default_model`、`default_device`、`cpu_threads`（1～32）、`queue_size`（1～256）、`port`（1～65535）を検査する。初版のhostは127.0.0.1に固定する。

## API

`GET /health` は ready/error、active/queued/completed、loaded=[model,device]、pid、default_model/default_device、acceptingを返す。readyは推論の準備完了を示し、モデルごとの品質を保証しない。

`GET /v1/models` は登録モデルのID・名称・固定revision・modalities・devices・backendを返す。devicesはAPIが受理する指定値であり、当該PCのCUDA利用可否を示さない。モデル未導入でも一覧には表示する。

`GET /openapi.json` と `/docs` で入力スキーマを確認できる。

`POST /v1/decisions`:

| フィールド | 意味 |
|---|---|
| model | 登録ID。省略時はサーバーのdefault_model |
| device | auto/cpu/cuda。autoはサーバーのdefault_deviceに従い、これもautoならCUDA可否で選ぶ |
| state | 最大24000文字。質問と合わせたテキストJSONはUTF-8で64KiB以内 |
| questions | ID→質問、1～8件。IDは1～100文字 |
| type | noul / choice / score |
| instructions | 質問ごとに3000文字以内 |
| criteria | noulは省略可、指定時true/false両方の説明。choiceは文字ID→説明の2～64件、数字だけのID不可。scoreは説明の2～10段階 |
| images | base64 data URL 最大4枚、PNG/JPEG/WebP。画像非対応モデルは拒否 |
| timeout_seconds | 1～300秒の整数、既定30。待ち時間と推論を含む |
| priority | interactive/normal/background、既定normal |

1説明は1000文字以内。HTTP本文は22MB以内。画像は1枚8MB/1600万画素、合計16MB。画像の実形式とdata URLのMIMEを照合する。推論の質問ごと8192トークンを超えた場合も切り捨てず拒否する。

正常応答には `answers`（Jeff互換）、`model`、`revision`、`device`、`request_id`、`queue_ms`、`load_ms`、`inference_ms`、`total_ms`、`usage`、`provider` が含まれる。total_msは受付後のサーバー内時間、queue_msは準備待ちを含む待ち時間、load_msは要求で必要だったモデル準備時間、inference_msは質問の準備・各forward・CPUへの結果転送を含む。画像検証や周辺処理があるため各時間の単純な合計とは一致しない。

| HTTP | 意味 |
|---|---|
| 403 | 管理認証失敗、または異なるOriginからの要求 |
| 413 | HTTP本文が上限超過 |
| 422 | 入力形式、未登録モデル、非対応画像、画像/トークン上限 |
| 429 | 有限待ち列が満杯。Retry-After: 1 |
| 503 | モデル未導入、CUDA利用不可、推論失敗、停止中 |
| 504 | 受付後の期限超過 |

呼出側は通信切断・期限切れ後の結果を使わない。推論のスレッドを止められない場合でも推論枠を解放しない。次の要求は実際の計算が終わってから開始する。モデルとdeviceは同じワーカー内で切替から全質問完了まで固定する。要求に操作コマンド実行機能は含まない。

管理停止は `POST /admin/shutdown`、`Authorization: Bearer ...`。起動時に生成する `runtime/control-<port>.token` をCLIが読む。tokenはGit・公開成果物へ入れない。loopbackの通常判定には認証を設けていないため、このサーバーを他ユーザーやネットワークへ公開しない。

## モデルとバックエンド

Jeff submoduleの固定revisionは `d0173b4ee317a46dee031421b713f3fc5f868cfe`。`git submodule update --init --recursive` で再現する。各モデル重みはconfigのrepo/revisionで取得し、`models/<name>/.fds-revision` が一致したものだけロードする。markerは導入元revisionの記録であり、全重みの署名・改ざん検証ではない。

既定のJeff Qwen3.5-2Bはテキストと画像を受け付ける。登録済みのQwen3.5-0.8BとGemma4 E2Bは必要時に `fds download --model <id>` で別途導入する。Gemmaの初版登録では画像を受け付けない。モデルは同時常駐せず、切替時に旧モデルを解放する。

Jeff対応モデルの追加はconfigへID/name/repo/40桁固定revision/checkpoint/backend/imagesを登録する。checkpointはこのプロジェクト内models配下に限る。Jeff以外のモデルを導入する場合は `warmup()`、`decide(DecisionRequest)`、`loaded` を提供するbackendを実装し、CLIのbackend生成箇所へ接続する。HTTPと待ち列の契約は維持する。

## 試験と実機測定

```powershell
cd C:\develop\FastDecisionServer
.\.venv\Scripts\python.exe -m compileall -q src dev/scripts
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
uv pip check --python .venv/Scripts/python.exe
.\.venv\Scripts\python.exe dev/scripts/benchmark.py --device cuda --samples 20 --output dev/testing/output/cuda.json
.\.venv\Scripts\python.exe dev/scripts/benchmark.py --device cpu --samples 3 --output dev/testing/output/cpu.json
```

単体試験はモデル重みを使わず、threading.Eventで長い推論を模擬し、取消後の枠保持と取り違えを検証する。測定スクリプトは短文と合成した赤い四角の256x256画像だけを使う。各ケースの最初の1件を予備として分離し、n>=20のみnearest-rank法のp95を出す。実機測定は精度評価・汎用的な速度保証とは区別する。