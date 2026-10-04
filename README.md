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

`GET /v1/models` は `installed`、`available`、`unavailable_reason` と、現在のサーバーで利用できる `devices` を返します。トップレベルにも `devices` と `capabilities_version: 1` を返します。未導入モデルは一覧に残し `available: false`、devicesは空配列です。GPUはサーバーのCUDA利用可否で判定し、クライアントPCのGPUとは無関係です。導入確認は設定・固定revision・重みファイルの存在によるもので、メモリ不足や破損ファイルまで成功を保証するものではありません。

要求ごとのCPU/GPU切替に対応します。起動時のDeviceは既定値であり固定ではありません。未導入モデルや未対応デバイスは推論待ち列へ入れる前に422で返します。推論失敗はJSONのdetailに理由を返し、期限超過は504になります。
