# API仕様

対象は FastDecisionServer v0.2.2。利用の入口は [README](../README.md)、起動・停止やモデル保持の操作は [利用ガイド](USER_GUIDE.md)、サーバー設定とバックエンド追加は [開発者ガイド](DEVELOPERS_GUIDE.md) を参照する。

## 接続先と共通の契約

待受は `127.0.0.1` のみ、既定ポートは8767。JSONを送受信する。稼働サーバーの `GET /openapi.json` と `GET /docs` で入力スキーマを確認できる。要求の未定義フィールドは拒否する。

| メソッド・パス | 用途 |
|---|---|
| `GET /health` | 稼働状態・実行状態・常駐一覧・有効な設定 |
| `GET /models` / `GET /v1/models` | 同じモデル一覧と利用可否 |
| `POST /v1/decisions` | 真偽・選択・尺度の判定 |
| `POST /models/load` | 登録済みモデルの常駐・再利用 |
| `POST /models/unload` | 指定した常駐モデルの解放 |
| `POST /admin/shutdown` | 認証付き停止 |

`Origin` ヘッダーがある場合、要求先のベースURL（末尾 `/` なし）と一致しない要求は403で拒否する。通常判定・モデル一覧・モデル管理には認証を設けていない。管理停止には後述のBearer tokenが必要。loopback以外や他ユーザー向けに公開する構成は対象外。

モデルは設定に登録したIDから選ぶ。要求本文・入力画像はディスクに保存しない。返す確率はモデルの推定であり、PC操作の実行権限や承認を与えるものではない。APIに操作コマンドを実行する機能はない。

## 稼働状態とモデル一覧

### `GET /health`

| フィールド | 意味 |
|---|---|
| `service` / `alias` / `version` / `pid` | サービス名・略称・稼働版・プロセスID |
| `ready` | 少なくとも1つ常駐モデルがあること。モデルの品質や全登録モデルの準備完了は保証しない |
| `running` / `accepting` | 新しい要求を受付中か |
| `error` | 直近の処理・準備失敗の理由。なければ `null` |
| `active` / `queued` / `completed` | 実行中ジョブ数（0/1）・待ちジョブ数・正常終了した推論数 |
| `loaded` | 直近選択の `[model, device]`。常駐がなければ `null` |
| `loaded_models` | 全常駐の `[{"model": ID, "device": "cpu" または "cuda", "revision": 固定版, "status": "ready"}]` |
| `generation` | 常駐構成の追加・解放で更新する世代番号 |
| `operation` | 実行中の操作。`action` は `warming` / `inference` / `load` / `unload` / `loading` / `unloading`。準備以外では `model`・`device` も含む。待機時は `null` |
| `model_management` | 稼働中の管理ポリシー。設定値は開発者ガイドを参照 |
| `default_model` / `default_device` | 稼働中の既定モデル・device |
| `cpu_threads` / `text_batch_size` | 稼働サーバーの設定値。判定時の実効値・分割は判定応答の `execution` を参照 |

モデル未導入などで準備に失敗してもhealthは応答し、常駐がなければ `ready: false` と `error` を返す。`active` と `operation` はモデル管理も含む実行状態で、`completed` は管理操作の回数ではない。

### `GET /models` / `GET /v1/models`

トップレベルは `object: "list"`、同内容の `data`・`models` 配列、`devices`、`default_device`、`capabilities_version: 2`、`loaded_models`、`model_management`、`generation`、`operation` を返す。`loaded_models` などの常駐情報はhealthと同じ意味。

各モデルのフィールドは次のとおり。

| フィールド | 意味 |
|---|---|
| `id` / `object` / `name` / `revision` | 登録ID・`"model"`・名称・固定revision |
| `modalities` / `backend` | 対応入力（`text`、対応時は `image`）とバックエンド |
| `installed` | `decision_config.json`・重みの存在と `.fds-revision` の一致による導入判定 |
| `available` / `unavailable_reason` | 導入済みかつ対応バックエンドなら `true`。利用可なら理由は `null` |
| `devices` | このサーバーで指定可能なdevice。未導入・未対応なら空配列 |
| `loadable` | `available` かつサーバーの `allow_load` が `true`。空き容量を保証しない |
| `load_allowed` / `unload_allowed` | サーバーのロード・解放許可 |
| `loaded_devices` | 実際に常駐している `cpu` / `cuda` の一覧 |
| `status.value` | `ready` / `unloaded` / `loading` / `unloading` / `unavailable` |

未導入モデルも登録一覧に残る。`available` はメモリ容量や重みの完全性まで確認する値ではない。GPU利用可否はサーバーのCUDA環境で判定し、クライアントPCのGPUとは無関係。トップレベルの `devices` はサーバーの利用可能deviceで、既定deviceを利用できない場合は `auto` を含めない。常駐済みなら `loadable: false` でも再利用できる。

## 判定要求

### `POST /v1/decisions`

| フィールド | 既定・制約 |
|---|---|
| `model` | 省略時はサーバーの `default_model`。1～100文字の登録ID |
| `device` | 既定 `auto`。`auto` / `cpu` / `cuda` |
| `state` | 必須の文字列。最大24000文字 |
| `questions` | 必須の「質問ID→質問」辞書、1～8件。IDは1～100文字 |
| `images` | 既定は空配列。base64 data URL、最大4枚 |
| `timeout_seconds` | 既定30。1～300秒の整数。待ち時間と実処理を含む |
| `priority` | 既定 `normal`。`interactive` / `normal` / `background` |
| `auto_unload` | 既定 `true`、真偽値。追加ロード時の解放検討を許すが、サーバーポリシーや承認を上書きしない |
| `approval_token` | 既定 `null`。承認後だけ付ける20～256文字のtoken |

`auto` はサーバーの `default_device` を参照し、それも `auto` ならCUDA利用可否で解決する。明示的なCPU指定はCPUで実行し、利用できないCUDA指定をCPUへ黙って変更しない。要求の全質問は同一モデル・実deviceで処理する。

質問の `type` は必須、`instructions` は既定空文字で最大3000文字。`type` と `criteria` の契約は次のとおり。

| `type` | `criteria` |
|---|---|
| `noul` | 省略または `null` が可能。指定時は `true`・`false` 両方の説明が必要 |
| `choice` | 必須。文字列ID→説明の辞書、2～64件。IDは1～100文字で数字のみは不可。さらにモデルの `max_options` を超える要求は拒否 |
| `score` | 必須。順序のある説明リスト、2～10段階 |

各説明は1000文字以内。`images` を除く全要求を検証後の既定値も含めてJSON化し、UTF-8で64KiB以内であることを検査する。HTTP本文は22,000,000バイト以内。

### 画像とトークンの制限

画像は `data:image/png;base64,`・`data:image/jpeg;base64,`・`data:image/webp;base64,` の形式のみ。外部URL・ファイルパス・画像非対応モデルへの入力は拒否する。data URLは1文字列10,666,700文字以内、base64本体は10,666,668文字以内で、復号後は1枚8,000,000バイト・合計16,000,000バイト以内、1枚16,000,000画素以内。

実際の画像形式とMIMEを照合して検証後、EXIFの向きを補正し、縦横とも1024px以内へ縦横比を保って縮小する。透明部分を白に合成し、RGBとしてモデルへ渡す。

モデル入力は1質問8192トークンを上限とし、超えた本文を切り捨てず拒否する。短いテキストは `text_batch_size`（既定8）の範囲で同じ要求の質問をまとめる。複数質問のpadding込み `input_ids` が1024要素を超えたらforward前に質問グループを二分する。長文そのものを分割・省略する処理ではなく、1質問は8192トークンまで単独で処理できる。画像付き要求は常に1質問ずつ。質問順・IDと回答の対応を維持する。

### 判定応答

| フィールド | 意味 |
|---|---|
| `answers` | 質問ID→Jeff互換の回答 |
| `model` / `revision` / `device` | 実際に使った登録ID・固定版・解決済みdevice |
| `request_id` | 正常応答の識別子 |
| `queue_ms` / `load_ms` / `inference_ms` / `total_ms` | 後述の時間、単位ミリ秒 |
| `usage.input_tokens` / `usage.output_tokens` | 実forwardに使った非padding入力トークンの合計 / `0` |
| `execution.batch_sizes` | 各forwardの質問数を実行順に並べた配列 |
| `execution.cpu_threads` | PyTorchの実効CPUスレッド数 |
| `execution.text_batch_token_limit` | 複数質問のpadding込み上限、`1024` |
| `provider` | `"fds"` |
| `management` | 推論前の常駐確保結果。管理成功応答の `success`・`model`・`device`・`load_ms`・`unloaded`・`loaded_models`・`generation` |

`noul` の回答は `type` と真である確率の `noul`。`choice` は `type`・`choice`（選択ID）・`confidence`・`probabilities`（選択ID→確率）。`score` は `type`・`score`（0始まりの尺度の期待値）・`confidence`・`probabilities`（尺度番号文字列→確率）・`legend`（尺度番号文字列→説明）を返す。

GPUのバッチ形状によって浮動小数の確率差が生じることがある。逐次条件で比較する場合はサーバーの `text_batch_size` を1にする。モデルの出力が有限の確率でなければ失敗として扱う。

## 待ち列・期限・計測

推論とモデル管理は単一ワーカーで直列実行する。起動時の準備を待ってからジョブを処理する。待ち列は既定16件で、満杯は429と `Retry-After: 1`。待ち要求は `interactive` → `normal` → `background` の順、同順位は受付順。モデル管理要求は `normal` として並び、実行中のジョブへ割り込まない。

`timeout_seconds` と `total_ms` の起点は、HTTP入力検証（通常判定ではモデル・deviceの事前検証も）を終え、待ち列へ投入する直前。HTTP本文の受信・解析やネットワーク往復全体を測る値ではない。

| 時間 | 範囲 |
|---|---|
| `queue_ms` | 起点からワーカー処理開始まで。起動時の準備待ちを含む |
| `load_ms` | 常駐確保・管理にかかった時間。再利用確認も含む。管理APIの解放処理でもこのフィールド名を使う |
| `inference_ms` | 常駐確保後の質問準備・各forward・CPUへの結果転送・回答変換。画像の事前正規化は含まない |
| `total_ms` | 起点から正常結果を返せる状態になるまでのサーバー内時間 |

画像正規化や周辺処理があるため、個別時間の単純な合計は `total_ms` と一致しない。直接実行と比べるときはモデルrevision・入力・実device・スレッド数・質問数を揃え、初回ロード・待ち列・推論・HTTP往復を分ける。

期限切れや切断は応答を取り消す。実行前なら処理をスキップするが、始まった計算や管理操作は中断できず続く場合がある。その間は実行枠を保持し、次のジョブを開始しない。呼出側は取消後の結果を使わず、結果不明の操作を自動再送しない。`/health` とモデル一覧で実状態を確認する。

## モデルの常駐管理

### ロード・アンロード要求

`POST /models/load` と `POST /models/unload` は同じ入力形式を使う。管理本文は16KiB（16,384バイト）以内。

```json
{"model":"jeff-qwen-2b","device":"cpu","auto_unload":true,"timeout_seconds":30}
```

`model` は必須の登録ID（1～100文字）、`device` は既定 `auto`、`timeout_seconds` は既定30・1～300の整数。`auto_unload` は既定 `true` の真偽値で、ロード時だけ使う。`approval_token` は既定 `null`、承認後だけ付ける20～256文字のtoken。HTTP操作はモデル重みを取得せず、アンロードも重みファイルを削除しない。

### CPU・GPU保持と許可

常駐キーは `(model, 解決済みdevice)`。同じモデルのCPU用・GPU用は別実体として同時保持でき、`auto` 用の枠はない。既定の常駐上限は全クライアント合計6組。同じモデルのCPU・GPUは2枠、3モデルを両方に保持するには6枠を使う。件数上限は物理メモリに収まる保証ではない。

CPUはRAM、GPUはVRAMに加えてロード時などのRAMも使う。モデル見積にRAM 2048MiB、GPU時はVRAM 1024MiBの既定余裕を足して検査する。空き容量・見積が不明ならロードを拒否する。設定の範囲・変更方法は [開発者ガイド](DEVELOPERS_GUIDE.md) を参照する。

| 状態・要求 | 動作 |
|---|---|
| 同じmodel/deviceが常駐済み | 再ロードせず再利用。ロード許可・解放承認は不要 |
| 未常駐でロード許可があり、常駐枠と必要な空き容量がある | 追加ロード。既存常駐を保持し、解放承認は不要 |
| 未常駐で `allow_load: false` | 403 `load_not_allowed`。起動時の既定モデル準備だけはこの許可に依存しない |
| 枠・容量が不足し、要求の `auto_unload` またはサーバーの `allow_auto_unload` が `false` | 409 `insufficient_capacity`。承認を求めず既存常駐とgenerationを保持 |
| 枠・容量が不足し、両方の自動解放指定が `true` だが `allow_unload: false` | 403 `unload_not_allowed`。既存常駐を解放しない |
| 枠・容量が不足し、自動解放と解放が許可されている | 使用が古い実体から候補を計画。解放しても不足なら409 `insufficient_capacity`、可能なら409 `approval_required` |
| 常駐モデルの明示アンロード | `allow_unload: true` と対象への承認が必要。`allow_auto_unload: false` でも可能 |
| 未常駐モデルのアンロード | 解放対象がないため、通常は承認なしで成功 |

要求の `auto_unload` は既定 `true`、サーバーの `allow_auto_unload` は既定 `false`。自動解放を検討するには両方が `true` である必要があり、古い `approval_token` でもサーバーの禁止を上書きできない。`allow_unload: false` は自動・明示の実際の解放を禁止する。常駐再利用・空きへの追加ロードに不要なtokenを付けると、承認の不一致で拒否される場合がある。

同時常駐でもCPUとGPUで別要求を並列推論しない。モデル管理と推論は同じワーカーで実行する。停止すると常駐は解放され、再起動時は既定モデルから準備する。停止前の全常駐構成は自動復元しない。有効なポリシーと常駐状態はhealthまたはmodelsで確認する。

### 成功応答と解放承認

管理成功応答の例（値は説明用）:

```json
{"success":true,"model":"jeff-qwen-2b","device":"cpu","load_ms":123.4,
 "unloaded":[],"loaded_models":[{"model":"jeff-qwen-2b","device":"cpu","revision":"00448e884cce9e34396d8c72e9a692b41383b1ee","status":"ready"}],
 "generation":1,"request_id":"要求ID","queue_ms":0.1,"total_ms":124.0}
```

解放承認が必要な409応答の例。ロード時のこの応答は、サーバーと要求が自動解放を許可している場合に返る。

```json
{"error":{"code":"approval_required","type":"model_management_error","message":"解放の確認が必要です。"},
 "detail":{"code":"approval_required","message":"解放の確認が必要です。",
 "approval":{"token":"一度限りの不透明token","expires_at":"UTCのISO8601時刻",
 "action":"load","model":"jeff-qwen-2b","device":"cuda",
 "unload":[{"model":"jeff-qwen-2b","device":"cpu"}],
 "reason":"空き容量または常駐上限のため既存モデルの解放が必要です。","generation":1}}}
```

クライアントは `approval.unload` を表示し、利用者が同意した場合だけ同じ操作に `approval_token` を付けて再申請する。既存常駐は承認前には解放しない。tokenはサーバー内で一時保持し、クライアントのログやディスクへ記録・保存しない。

サーバーはaction・model・解決済みdevice・解放一覧・generationを照合する。承認の期限は既定120秒で、一度使用したtoken、別操作・別対象・不明なtokenは409 `approval_invalid`。期限切れ・世代変更・解放候補の変更があり、なお解放が必要なら新しい409 `approval_required` で再確認する。解放が不要になった場合や期限切れtokenが既に破棄された場合は `approval_invalid` となることがある。確認なしに再実行しない。

承認後は提示対象だけを解放し、実際の空き容量を再確認する。不足やロード失敗でも追加の無断解放はせず、無関係の常駐を保持する。既に承認して解放した実体を自動で復元する契約ではない。

通常判定の暗黙ロードにも同じ許可・承認を適用し、承認前に質問を推論しない。管理APIで先に準備すると、利用者の承認待ちを推論の計測から分けられる。

## エラー

| HTTP | 意味 |
|---|---|
| 403 | 管理停止の認証失敗、異なるOrigin、ロード・解放の許可なし |
| 409 | 解放承認が必要・不正、または容量不足 |
| 413 | HTTP本文の上限超過 |
| 422 | 入力形式、未登録・未導入モデル、非対応device・画像、画像・トークン・選択肢上限 |
| 429 | 有限待ち列が満杯。`Retry-After: 1` |
| 500 | 想定外の処理失敗 |
| 503 | 推論・ロード失敗、メモリ情報取得不能、停止中 |
| 504 | 待ち時間を含む処理期限超過 |

管理エラーは `error: {code,type,message}` と `detail: {code,message,approval?}` を返し、`type` は `model_management_error`。その他のエラーは `detail` に理由を返す。入力スキーマ違反の `detail` は `loc`・`msg`・`type` の配列で、入力値自体は含めない。

| `code` | HTTP | 意味 |
|---|---|---|
| `approval_required` | 409 | 解放対象の承認が必要 |
| `approval_invalid` | 409 | 不明・使用済み・不一致などの承認 |
| `insufficient_capacity` | 409 | 自動解放禁止、見積上または解放後の空き不足。自動解放禁止時は不足容量（余裕込み）または件数上限をメッセージに含め、`approval` は付けない |
| `load_not_allowed` / `unload_not_allowed` | 403 | サーバーの許可なし |
| `model_unknown` / `model_not_installed` | 422 | 未登録、未導入または固定版不一致 |
| `backend_unsupported` / `device_unavailable` | 422 | 未対応バックエンド、指定device利用不能 |
| `memory_unavailable` / `memory_estimate_missing` | 503 | 実空き容量またはモデル見積が取得できない |
| `model_load_failed` | 503 | モデルロード失敗。承認対象外の常駐モデルは維持 |

## 管理停止

`POST /admin/shutdown` に `Authorization: Bearer <管理停止token>` を付ける。tokenは起動時に生成し、CLIが `runtime/control-<port>.token` を読む。Gitや公開成果物へ含めない。このtokenと、モデル解放用の `approval_token` は別物。

正常応答は `{"stopping": true}`。新規受付を閉じ、待ち要求を失敗として返し、既に実行している計算が終わってからサーバーを終了する。応答を受け取った時点ではプロセス終了まで完了しているとは限らない。
