# 開発者ガイド

対象は FastDecisionServer v0.2.2。環境構築、設定、モデル追加、検証方法を記載する。利用の入口は [README](../README.md)、起動・停止と日常操作は [利用ガイド](USER_GUIDE.md)、HTTPの契約は [API仕様](API.md) を参照する。

## 環境構築

Windows / Python 3.12、uv、Gitを使用する。プロジェクト専用の `.venv/` と `models/` を使い、他プロジェクトの環境・モデルを変更しない。`.venv/`・`models/`・`runtime/` はGit対象外。

ソースを初めて取得する場合は、privateリポジトリへアクセスできるアカウントでsubmoduleも取得する。

```powershell
cd C:\develop
git clone --recurse-submodules https://github.com/KotorinChunChun/FastDecisionServer.git FastDecisionServer
```

取得したプロジェクトで環境とモデルを準備する。

```powershell
cd C:\develop\FastDecisionServer
pwsh -NoProfile -File .\dev\scripts\setup.ps1 -Device cpu
.\.venv\Scripts\fds.exe download --model jeff-qwen-2b
.\.venv\Scripts\fds.exe serve --device cpu
```

[setup.ps1](../dev/scripts/setup.ps1) は専用環境とJeff submoduleを準備し、依存を導入して `uv pip check` を実行する。CPU専用版はCUDAやNVIDIA GPUを必要としない。GPU版は導入時に `-Device cuda` を指定し、CUDA 13.0対応のPyTorchを使うためのドライバーを用意する。Python依存の固定版は [pyproject.toml](../pyproject.toml) と同スクリプトを参照する。

`fds serve` はフォアグラウンドで起動する。非表示での起動と状態確認は [利用ガイド](USER_GUIDE.md) を参照する。起動時の既定モデル準備が終わってから待ち要求を処理する。ポートを確保した後に管理停止tokenを書くため、二重起動で既存プロセスのtokenを上書きしない。

## 設定

公開設定は [config.json](../config.json)、ローカル上書きはGit対象外の `config.local.json`。上書きはトップレベル単位で、`models` や `model_management` の内部を再帰的にマージしない。ローカルの `models` は辞書全体を置き換えるため、モデルを上書きするときは `memory_mb` も含める。`model_management` の未指定項目はプログラムの既定ポリシーで補完する。

設定は起動時に読み込み、`--model`・`--device`・`--port` があれば対応する既定値を上書きする。稼働中の設定変更は再起動後に反映する。検査は [cli.py](../src/fds/cli.py)、ポリシーの既定は [management.py](../src/fds/management.py) にある。

### 一般設定

以下の既定値は公開 `config.json` の値。

| キー | 既定値 | 範囲・意味 |
|---|---|---|
| `host` | `127.0.0.1` | このアドレスのみ。ネットワーク公開は非対応 |
| `port` | `8767` | 整数1～65535 |
| `default_model` | `jeff-qwen-2b` | `models` に登録済みのID |
| `default_device` | `auto` | `auto` / `cpu` / `cuda`。要求で個別指定できる |
| `cpu_threads` | `4` | 整数1～32。Jeffロード時に設定するPyTorchのCPUスレッド数 |
| `text_batch_size` | `8` | 整数1～8。同じ要求内で一緒に推論するテキスト質問数 |
| `queue_size` | `16` | 整数1～256。実行待ち要求の上限 |

`auto` は要求の実行時に既定deviceを参照し、それも `auto` ならサーバーのCUDA利用可否で `cuda` または `cpu` に解決する。明示した `cpu` をGPUへ変更せず、利用不能な `cuda` 指定は拒否する。

テキストの複数質問はpadding込みの `input_ids` が1024要素を超えるとforward前に質問グループを二分する。本文を切断・省略しない。1質問は8192トークンまでで、画像付き要求は常に1質問ずつ処理する。`text_batch_size: 1` で逐次方式にできる。複数のHTTP要求をまとめる設定ではなく、単一ワーカーと取消後の実処理保持は維持する。実際の分割数は判定応答の `execution.batch_sizes` で確認する。

### モデル管理設定

以下は `model_management` 内のキー。真偽値に文字列や数値は使えない。ここにないキーは設定エラーとなる。

| キー | 既定値 | 範囲・意味 |
|---|---|---|
| `allow_load` | `true` | クライアントによる未常駐モデルの追加ロードを許可 |
| `allow_unload` | `true` | 常駐モデルの解放を許可。実際の解放には別途承認が必要 |
| `allow_auto_unload` | `false` | 追加ロード時に既存常駐の解放を検討してよいか |
| `max_loaded_models` | `6` | 整数1～32。全クライアント共通のモデル・device組数の上限 |
| `ram_reserve_mb` | `2048` | 整数0～1048576。ロード時に残すRAM余裕、単位MiB |
| `vram_reserve_mb` | `1024` | 整数0～1048576。GPUロード時に残すVRAM余裕、単位MiB |
| `approval_ttl_seconds` | `120` | 整数10～600。解放承認tokenの有効秒数 |

要求の `auto_unload` は既定 `true` だが、サーバーの `allow_auto_unload` と両方が `true` の場合だけ自動解放を検討する。さらに `allow_unload` と利用者の承認が必要になる。常駐再利用と空きへの追加ロードには解放承認を求めない。起動時の既定モデル準備は `allow_load` に依存せず、ロード禁止でも常駐再利用は可能。詳しい状態遷移・不足時の応答は [API仕様](API.md) を参照する。

### モデル登録とメモリ見積

`models` は登録IDからモデル設定への辞書。IDは英数字・`_`・`.`・`-` の1～100文字。Jeffモデルには以下を指定する。

| フィールド | 内容・制約 |
|---|---|
| `name` | 一覧に表示する名称 |
| `repo` | モデル取得元のリポジトリ |
| `revision` | 40桁の16進固定revision |
| `checkpoint` | このプロジェクト内の `models/` 配下の保存先 |
| `backend` | `jeff`。別方式はバックエンド実装とCLI接続も必要 |
| `images` | 画像入力を受け付けるか |
| `memory_mb.cpu` / `memory_mb.cuda` | 各deviceの `{ "ram": 数値, "vram": 数値 }`。単位MiB |

メモリ見積の値は有限の数値で0～1048576、`ram` は正数。CPUの `vram` は0、CUDAの `vram` は正数。未知のdevice・項目は許可せず、利用するdeviceの見積が未設定ならロードを拒否する。GPUロード中に必要なRAMも見積に含める。

空き容量はWindowsの `GlobalMemoryStatusEx` とCUDAの `mem_get_info` で取得し、見積と予約余裕を合わせて検査する。空き取得不能時は架空の容量で続行しない。見積は成功保証ではなく、常駐枠を増やしてもRAM・VRAM検査は省略しない。承認して解放した後も再検査し、不足やロード失敗を理由に承認外のモデルを追加解放しない。

## モデルとバックエンド

Jeff submoduleの固定revisionは `d0173b4ee317a46dee031421b713f3fc5f868cfe`。`git submodule update --init --recursive` で再現する。モデル重みは設定の `repo`・`revision` から `fds download --model <id>` で取得する。HTTPのロード操作は重みを取得しない。

| 登録ID | モデル | 受け付ける入力 |
|---|---|---|
| `jeff-qwen-2b` | Jeff Qwen3.5-2B（既定） | テキスト・画像 |
| `jeff-qwen-0.8b` | Jeff Qwen3.5-0.8B | テキスト・画像 |
| `jeff-gemma-e2b` | Jeff Gemma4 E2B | テキスト |

各モデルの固定版・保存先は [config.json](../config.json) を参照する。`decision_config.json`、重みファイル、設定のrevisionに一致する `.fds-revision` を確認してからロードする。markerは導入元revisionの記録であり、全重みの署名・改ざん検証ではない。導入判定はメモリ容量やファイルの完全性まで保証しない。

別のバックエンドを接続するときは `warmup()`、`decide(DecisionRequest)`、`loaded` を提供し、CLIの生成箇所へ接続する。常駐管理を実装する場合は `manage(action, ModelOperation)`・`status()`・`capabilities()`・要求の事前検証もJeff実装を参照する。HTTP、待ち列、エラー、取消の契約は [API仕様](API.md) を維持する。

モデル本体と `max_options` はモデル・deviceのエントリーごとに保持する。推論と管理の変更は単一ワーカーだけが行い、一覧は短いロックによるスナップショットで読む。承認確認・token消費・解放・ロードは同じジョブで処理する。応答が期限切れや切断で破棄されても、実処理が終わるまで実行枠を保持する。

## 試験と実機測定

単体試験はモデル重みを使わない。`threading.Event` で長い推論を模擬し、取消後の枠保持・モデル/deviceの取り違え・管理ポリシー・バッチ分割などを検証する。

```powershell
cd C:\develop\FastDecisionServer
.\.venv\Scripts\python.exe -m compileall -q src dev/scripts
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
uv pip check --python .venv/Scripts/python.exe
```

実モデルの測定は、入力・モデルrevision・実device・CPUスレッド数・質問数を揃え、初回ロード、予備実行、待ち列、推論、HTTP往復を分ける。GPUのバッチ形状により浮動小数の確率差が生じることがあり、逐次条件との比較には `text_batch_size: 1` を使う。速度測定を日本語全般の精度や汎用的な速度の保証として扱わない。

| スクリプト | 用途・前提 |
|---|---|
| [benchmark.py](../dev/scripts/benchmark.py) | 稼働FDSでQwen2Bの固定短文と合成した赤い四角の256×256画像を測定。各ケースの最初の1件を予備として分離し、n≥20でnearest-rank法のp95を計算 |
| [benchmark_execution.py](../dev/scripts/benchmark_execution.py) | サービスへ接続せず、1/8質問の合成短文でFDSバックエンドと比較用の直接一括を測定。FDS側は設定中のバッチ方式を使う。paddingを除いた入力の一致と確率差も確認 |
| [benchmark_transport.py](../dev/scripts/benchmark_transport.py) | 同一入力の直接一括と専用FDSのHTTPを比較。比較対象を事前に常駐させ、他の推論負荷がない条件を確認し、CPUスレッド設定を揃える。既定URLはポート18767 |

`benchmark.py` の例（測定対象FDSを起動済みで実行）:

```powershell
cd C:\develop\FastDecisionServer
.\.venv\Scripts\python.exe dev/scripts/benchmark.py --device cuda --samples 20 --output dev/testing/output/cuda.json
.\.venv\Scripts\python.exe dev/scripts/benchmark.py --device cpu --samples 3 --output dev/testing/output/cpu.json
```

直接実行を含む測定は別プロセスにもモデルをロードするため、その分のメモリが必要になる。測定記録は `dev/testing/`、変更の設計・判断・結果は `dev/` の実装記録へ残す。要求本文や利用者の画像を検証資料として保存しない。
