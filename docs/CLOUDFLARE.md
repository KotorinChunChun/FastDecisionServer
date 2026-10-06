# Cloudflare Clefの設定

FDSからCloudflare Workers AIのClef／Clef-Flashを呼び出す。ローカルへのモデル導入は不要。Jeffを選んだ判定はローカルで実行し、Clefを選んだ判定の本文・質問・正規化画像はCloudflareへ送信する。利用量はCloudflareアカウントに計上される。

## アカウントIDとトークンの入力

Workers AIを利用できるアカウントIDとAPIトークンを用意する。権限と作成方法は[Cloudflare公式REST API手順](https://developers.cloudflare.com/workers-ai/get-started/rest-api/)を参照する。トークンをチャット・ソース・config.jsonへ貼り付けない。

専用Python環境を導入済みなら、PowerShellで次を実行する。

```powershell
cd C:\develop\FastDecisionServer
pwsh -NoProfile -File .\dev\scripts\start-cloudflare.ps1 -Restart
```

アカウントID、続いてトークンを入力する。トークン入力は画面に表示しない。起動済みなら正常終了を待って再起動し、既定モデルとdeviceを引き継ぐ。停止すると、それまでのローカル常駐は解放され、既定モデルだけが再準備される。終了が60秒を超えた場合は強制終了せず停止するため、既存処理の終了後に再実行する。

クラウドだけで起動し、Jeffの起動時ロードを省略する場合:

```powershell
pwsh -NoProfile -File .\dev\scripts\start-cloudflare.ps1 -Restart -Model cloudflare-clef-flash
```

スクリプトは子プロセスに `CLOUDFLARE_ACCOUNT_ID` と `CLOUDFLARE_AUTH_TOKEN` を渡す。ファイルやユーザー環境変数へ永続保存しないので、通常の別起動・ログオン時自動起動には引き継がれない。再起動時はこのスクリプトで入力し直す。起動時にはCloudflare推論を送らず、認証文字列の形式だけを確認する。環境変数名をconfig.local.jsonで変更した構成はこの補助スクリプトの対象外で、その名前を設定した親プロセスから起動する。

## 判定と接続確認

```powershell
$body = @{
    model='cloudflare-clef-flash'; device='cloud'; state='猫は動物です。'
    questions=@{ animal=@{ type='noul'; instructions='猫は動物ですか？' } }
    timeout_seconds=60
} | ConvertTo-Json -Depth 8
Invoke-RestMethod http://127.0.0.1:8767/v1/decisions -Method Post -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body))
```

この操作はCloudflareへ1回送信する。成功時は `device: cloud`、`provider: cloudflare` と判定結果が返る。Clefには `model='cloudflare-clef'` を指定する。deviceは `auto` も使えるが `cpu`・`cuda` は拒否する。モデル選択肢を固定している既存クライアントは、これらのIDへの対応が別途必要。

`GET /models` の `available: true`、`GET /health` の `cloud_configured: true` は認証文字列が設定されていることだけを示し、権限・残高・実接続を保証しない。未設定は `cloudflare_not_configured`、認証・権限エラーは `cloudflare_auth_failed`、利用上限・混雑は `cloudflare_rate_limited`。トークンを更新したらFDSを再起動する。接続先はCloudflare公式APIに固定し、環境変数のHTTPプロキシは使用しない。

通信失敗・期限切れ時は処理済みの可能性がある。自動再送やJeffへの自動切替は行わない。FDSの応答取消でCloudflare側の計算を止められるとは限らない。

## 実行と入力の違い

クラウドはローカル常駐枠・RAM/VRAM見積の対象外で、Jeffの常駐構成を変更しない。download・load・unloadは不要。推論の待ち列と単一ワーカーは共有する。

FDSの本文24000文字・質問1～8件などの共通制限を維持する。クラウドの質問IDは英数字・`_`・`.`・`-` に限る。画像は共通の正規化後にPNGとして送信し、さらに1枚4MiB、合計8MiB、要求全体13MiBの上限を適用する。Cloudflare側では長いstateがトークン上限に合わせて切り捨てられる場合がある。Jeffの8192トークン超過時の拒否・バッチ設定はクラウドには適用しない。[Clef公式仕様](https://developers.cloudflare.com/workers-ai/models/clef/)・[Clef-Flash公式仕様](https://developers.cloudflare.com/workers-ai/models/clef-flash/)を参照する。

応答の `revision` は固定版を確認できないため `null`、`load_ms` は0、`inference_ms` は上流との通信を含む時間。ローカルの純粋な推論速度と直接比較しない。詳細は[API仕様](API.md)を参照する。
