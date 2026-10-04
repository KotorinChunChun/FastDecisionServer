# FastDecisionServer（fds）

ローカルアプリから共用する判定サーバーです。短縮名は **fds**。既定の Jeff Qwen3.5-2B で真偽・選択・尺度を返し、対応モデルでは画像も入力できます。DesktopAgent とは独立して起動します。

## 起動

Python 3.12 と uv、Git を用意し、初回に専用環境と固定モデルを導入します。

```powershell
cd C:\develop\FastDecisionServer
pwsh -NoProfile -File .\dev\scripts\setup.ps1 -Device cpu
.\.venv\Scripts\fds.exe download
pwsh -NoProfile -File .\dev\scripts\fds.ps1 -Command start -Device cpu
```

NVIDIA GPU を使う環境では、導入時の `-Device cuda` で CUDA 13.0 対応 PyTorch を選びます。起動時は `-Device auto`（CUDAが利用可能ならGPU、なければCPU）、`cpu`、`cuda` を選べます。GPU版の環境でも `cpu` は CPU で実行します。CUDA指定時にCUDAが使えなければ明示的に失敗します。

待受は [127.0.0.1:8767](http://127.0.0.1:8767/health) のみです。`ready: true` が準備完了を示します。モデル未導入でも health は開き、理由を `error` で確認できます。

```powershell
pwsh -NoProfile -File .\dev\scripts\fds.ps1 -Command status
pwsh -NoProfile -File .\dev\scripts\fds.ps1 -Command stop
```

停止は受付を閉じ、現在の計算を終えてから終了します。期限切れ・切断も既に始まった計算そのものは中断しません。状態不明の要求を自動再送しないでください。

## 判定例

```powershell
$body = @{
    model='jeff-qwen-2b'; device='cpu'; state='猫は動物です。'
    questions=@{ animal=@{ type='noul'; instructions='猫は動物ですか？' } }
    timeout_seconds=30; priority='interactive'
} | ConvertTo-Json -Depth 8
Invoke-RestMethod http://127.0.0.1:8767/v1/decisions -Method Post -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body))
```

[対話API仕様](http://127.0.0.1:8767/docs)・[モデル一覧](http://127.0.0.1:8767/v1/models)。質問と応答は Jeff 互換です。`noul` は真偽の確率、`choice` は候補ごとの確率、`score` は尺度から得た値を返します。確率はモデルの推定であり、操作の実行権限や承認を与えるものではありません。

画像は PNG/JPEG/WebP の base64 data URL を `images` 配列へ渡します。1枚8MB・1600万画素、合計16MB、最大4枚。長辺1024px以内へ縮小し、透明部分を白にします。ファイルパスやURLは受け付けず、要求本文や画像を保存しません。

モデルは `config.json` へ登録したIDから選び、要求ごとにモデルとdeviceを固定します。待ち列は既定16件、満杯は429。`interactive`、`normal`、`background` の順に待ち要求を処理し、同順位は受付順です。実行中の要求には割り込みません。

[構築・API・モデル追加](docs/DEVELOPERS_GUIDE.md) / [検証結果](dev/testing/v0.1.0-results.md)
## ソースと連携状況

ソースは [private GitHub](https://github.com/KotorinChunChun/FastDecisionServer) に保存しています。2026-10-04、共通アプリ一覧とDevLauncherの原本へ登録済みです。ランチャー配布版への反映は別工程です。

DesktopAgent v0.17.0で6用途のJeff/Luna切替へ接続しました。合成日本語26件では、不確かな判定をLunaへ切り替える既定構成が25/26、Luna固定が26/26でした。24件でLunaへの切替が必要で、アプリ全体の高速化は確認できていません。単純な猫・図形のスモーク速度を日本語全般の品質保証として扱わないでください。詳細はDesktopAgentの dev/testing/v0.17.0-results.md を参照してください。

## クライアント用の利用可否

`GET /v1/models` は `installed`、`available`、`unavailable_reason` と、現在のサーバーで利用できる `devices` を返します。トップレベルにも `devices` と `capabilities_version: 2` を返します。未導入モデルは一覧に残し `available: false`、devicesは空配列です。GPUはサーバーのCUDA利用可否で判定し、クライアントPCのGPUとは無関係です。導入確認は設定・固定revision・重みファイルの存在によるもので、メモリ不足や破損ファイルまで成功を保証するものではありません。

要求ごとのCPU/GPU切替に対応します。起動時のDeviceは既定値であり固定ではありません。未導入モデルや未対応デバイスは422で返します。推論失敗はJSONのdetailに理由を返し、期限超過は504になります。

## モデルの常駐管理（v0.2.2）

導入済みモデルは `GET /models` または `GET /v1/models` で確認できます。`installed/available` は固定版の導入状況、`loadable` は導入済みでサーバーがロードを許可している状態です。`loaded_devices` は実際に常駐している CPU/GPU、`status.value` は ready/unloaded/loading/unloading/unavailable を表します。常駐とロード可能は別です。

`POST /models/load` と `POST /models/unload` に `model`、`device`（auto/cpu/cuda）を送ります。モデル重みは取得しません。ロード済みの再利用、空きへの追加ロード、未常駐モデルのアンロードは確認不要です。既存モデルを解放する操作は常に HTTP 409 `approval_required` を返し、クライアントが対象を表示して承認後に token 付きで再申請します。`auto_unload: true` でも承認を省略しません。

同じモデルのCPU用とGPU用を同時にメモリへ保持できます。CPU用・GPU用は別々の常駐実体で、切替先が既に常駐していればそのまま再利用します。切替先を追加ロードする場合も、必要なRAM・VRAMと常駐枠に余裕があれば、切替元を残して確認なしでロードします。CPU/GPUの切替自体がアンロードを必須にする仕様ではありません。

常駐上限は `model_management.max_loaded_models` で指定し、モデルとデバイスの組み合わせを1枠と数えます。同じモデルをCPU・GPUの両方に保持すると2枠、3モデルを両方に保持するには6枠が必要です。例えば上限を3枠にした場合、3モデルがCPUに常駐した状態で1モデルのGPU用を追加すると、メモリに余裕があっても上限を超えます。稼働サーバーの上限は `GET /models` または `GET /health` の `model_management.max_loaded_models` で確認できます。追加ロードにはモデルの必要量に加え、RAM 2048MiB、GPU使用時はさらにVRAM 1024MiBの既定の余裕を確保します。

追加ロードに必要な常駐枠またはメモリが不足する場合、自動解放を許可していれば使用が古い常駐実体から解放候補にし、利用者の承認を求めます。自動解放を許可していなければ、既存モデルを保持したまま追加ロードを拒否します。共用サーバーなので、他のクライアントも使う可能性のある実体を無断で解放しません。メモリ見積や空き容量が不明ならロードを拒否し、OOMが発生しても無断で追加解放しません。アンロードはメモリ上の実体の解放であり、導入済みモデルのファイルは削除しません。

サーバー設定 `model_management.allow_load/allow_unload` で許可を変更できます。ロード禁止でも既に常駐している組み合わせの推論は使えます。設定は次回起動時に反映します。クライアントの `auto_unload: false` は自動解放を禁止します。通常推論の暗黙ロードにも同じ許可と承認が適用されます。

CPU用とGPU用を同時に保持していても、管理・推論は同じワーカーで直列実行します。CPUとGPUで別の要求を同時に推論する仕様ではありません。期限切れや切断で応答を破棄しても処理が続く場合があるため、自動再送せず一覧と health で実状態を確認してください。承認 token は記録・保存しません。

## 実行速度（v0.2.1）

短いテキストの複数質問は、同じ要求の中で最大8件をまとめて計算します。長文は小さく分割し、画像は1件ずつ計算します。モデル・実行デバイス・質問内容は変更しません。従来と同じ逐次方式が必要な場合は設定の `text_batch_size` を1にして再起動します。

直接実行との速度差を調べるときは、応答の `device` が同じか確認してください。CPU指定をサーバーがGPUへ変更することはありません。`queue_ms` は他要求を待った時間、`inference_ms` は実際の推論時間です。GPU同士・同じ入力でも比べる質問数や初回ロードの有無が違えば速度差が出ます。詳細は[開発者ガイド](docs/DEVELOPERS_GUIDE.md)を参照してください。

今回の合成短文8質問では、GPUの逐次→一括がQwen0.8Bで約7.2倍、Qwen2Bで約6.1倍高速化しました。Qwen0.8Bの直接一括とHTTPの差は中央値約2msです。CPUや1質問で同じ改善を保証するものではありません。[測定条件・確率差・再現方法](dev/testing/v0.2.1-results.md)を参照してください。

## CPU/GPU切替時の保持（v0.2.2）

既定の常駐上限を6組（3モデル×CPU/GPU）に増やし、サーバーの `model_management.allow_auto_unload` をfalseにしました。ロード済みなら切替先を再利用し、未ロードなら空き容量の範囲で追加します。クライアントが `auto_unload: true` を送っても、サーバーが自動解放を禁止していれば既存モデルを解放しません。

メモリや常駐枠が不足した場合は、既存モデルを残して409 `insufficient_capacity`を返します。応答に不足した容量または件数上限を含めます。6組は件数の上限であり、16GBのVRAMに全モデルが同時に収まる保証ではありません。手動アンロードは引き続き対象確認と承認が必要です。サーバーを停止すると常駐モデルは解放されます。
