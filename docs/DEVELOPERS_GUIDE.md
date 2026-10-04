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

`GET /v1/models` は登録モデルのID・名称・固定revision・modalities・devices・backendを返す。devicesは当該サーバーのCUDA利用可否を反映する。モデル未導入でも一覧には表示する。

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
| 503 | 推論失敗、ロード失敗、メモリ情報取得不能、停止中 |
| 504 | 受付後の期限超過 |

呼出側は通信切断・期限切れ後の結果を使わない。推論のスレッドを止められない場合でも推論枠を解放しない。次の要求は実際の計算が終わってから開始する。モデルとdeviceは同じワーカー内で切替から全質問完了まで固定する。要求に操作コマンド実行機能は含まない。

管理停止は `POST /admin/shutdown`、`Authorization: Bearer ...`。起動時に生成する `runtime/control-<port>.token` をCLIが読む。tokenはGit・公開成果物へ入れない。loopbackの通常判定には認証を設けていないため、このサーバーを他ユーザーやネットワークへ公開しない。

## モデルとバックエンド

Jeff submoduleの固定revisionは `d0173b4ee317a46dee031421b713f3fc5f868cfe`。`git submodule update --init --recursive` で再現する。各モデル重みはconfigのrepo/revisionで取得し、`models/<name>/.fds-revision` が一致したものだけロードする。markerは導入元revisionの記録であり、全重みの署名・改ざん検証ではない。

既定のJeff Qwen3.5-2Bはテキストと画像を受け付ける。登録済みのQwen3.5-0.8BとGemma4 E2Bは必要時に `fds download --model <id>` で別途導入する。Gemmaの初版登録では画像を受け付けない。v0.2.0ではモデルとdeviceごとに複数常駐し、解放が必要な場合だけ承認付きで切り替える。

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
## v0.2.0 管理APIの契約

`GET /models` と `GET /v1/models` は同じ応答を返す。旧 `models` 配列に加えて `object: "list"` と同内容の `data` 配列を返す。各モデルに `loadable/load_allowed/unload_allowed/loaded_devices/status.value`、トップレベルに `loaded_models/model_management/generation/operation` を追加した。旧属性は維持する。

`loaded_models` は `[{model,device,revision,status:"ready"}]`。`health.loaded` は直近選択の `[model,device]` という旧形式を維持し、全常駐一覧は `loaded_models` にある。`health.ready` は常駐モデルが少なくとも1つあること、`running` は受付中、`active` と `operation` は実行状態を表す。空のままロードに失敗した場合は ready=false と理由を返す。

ロード/アンロードの本文:

```json
{"model":"jeff-qwen-2b","device":"cpu","auto_unload":true,"timeout_seconds":30}
```

`model` は必須の登録ID、`device` 既定auto、`auto_unload` 既定true、`timeout_seconds` は1～300の整数。`approval_token` は承認後だけ付ける。管理本文は16KiB以内。CPU/GPUごとのモデル登録が必要という意味ではなく、常駐キーが(model,解決済みdevice)になる。

成功応答:

```json
{"success":true,"model":"jeff-qwen-2b","device":"cpu","load_ms":123.4,
 "unloaded":[],"loaded_models":[{"model":"jeff-qwen-2b","device":"cpu","revision":"固定版","status":"ready"}],
 "generation":1,"request_id":"要求ID","queue_ms":0.1,"total_ms":124.0}
```

解放前の409応答:

```json
{"error":{"code":"approval_required","type":"model_management_error","message":"解放の確認が必要です。"},
 "detail":{"code":"approval_required","message":"解放の確認が必要です。",
 "approval":{"token":"一度限りの不透明token","expires_at":"UTCのISO8601時刻",
 "action":"load","model":"jeff-qwen-2b","device":"cuda",
 "unload":[{"model":"jeff-qwen-2b","device":"cpu"}],
 "reason":"空き容量または常駐上限のため既存モデルの解放が必要です。","generation":1}}}
```

利用者が `approval.unload` に同意した場合だけ、同一操作へ `approval_token` を付けて再申請する。target/device/action/解放一覧/generationはサーバーが検証する。期限は既定120秒。一度使用したtoken、別対象や別操作のtokenは409 `approval_invalid`。期限や常駐状態の変更は状態を変更せず新しい `approval_required` で再確認する。期限切れtokenが管理上破棄済みの場合もapproval_invalidとなるので、確認なしの実行はせず改めて申請する。

通常の `POST /v1/decisions` にも `auto_unload` と `approval_token` を追加した。推論前に同じ検査を通し、承認前には質問を実行しない。管理APIで準備を済ませてから推論すれば、利用者の承認待ちを計測から除外できる。期限と切断は応答だけを取り消すため、結果不明の管理・推論を自動で再送しない。

| code | HTTP | 意味 |
|---|---|---|
| approval_required | 409 | 解放対象の承認が必要 |
| approval_invalid | 409 | 不明、使用済み、別操作・別対象の承認 |
| insufficient_capacity | 409 | 自動解放禁止、または見積上/解放後の空き不足 |
| load_not_allowed / unload_not_allowed | 403 | サーバーの許可なし |
| model_unknown / model_not_installed | 422 | 未登録、未導入または固定版不一致 |
| backend_unsupported / device_unavailable | 422 | 未対応バックエンド、CPU/GPU指定不能 |
| memory_unavailable / memory_estimate_missing | 503 | 実空き容量または見積が取得できない |
| model_load_failed | 503 | ロードに失敗。無関係の常駐モデルは維持 |

ポリシーの `max_loaded_models` は1～32、RAM/VRAM余裕は0～1048576MiB、承認期限は10～600秒。登録モデルの `memory_mb.cpu/cuda` は各 `{ram,vram}` をMiBで指定する。CPUのvramは0、ramとCUDAのvramは正数。公開設定の見積は保守的な計画値であり成功保証ではない。GPUロード中のRAMも見積に含める。実空きはWindows GlobalMemoryStatusEx/CUDA mem_get_infoで取得し、解放後にも再確認する。見積不足で失敗しても承認対象を追加しない。古いlocal設定がmodels全体を置き換える場合は、memory_mbも明示的に設定する。

起動時の既定モデル準備はサーバー自身の処理としてロード許可に依存しない。クライアントの追加ロードは許可に従う。ロード禁止でも常駐再利用は許可される。アンロード禁止なら自動追放と明示解放の両方を拒否する。

常駐管理を実装するバックエンドは既存の `warmup/decide/loaded` に加えて `manage(action, ModelOperation)/status()/capabilities()` を持つ。モデル本体・max_optionsはエントリーごとに保持する。変更は単一ワーカー、一覧の読み取りは短いロックによるスナップショットで行う。承認確認・token消費・解放・ロードは同じジョブ中で行い、期限切れ後も実処理終了まで実行枠を保持する。
