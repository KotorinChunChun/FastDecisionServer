# FastDecisionServer（fds）

ローカルアプリから共用する判定サーバーです。短縮名は **fds**。JeffモデルとCloudflare Clefで真偽・選択・尺度を返し、対応モデルでは画像も入力できます。DesktopAgentやTestJeffとは独立して起動します。

## 起動

WindowsにPython 3.12、uv、Gitを用意します。初回に専用環境と既定のJeff Qwen3.5-2Bを導入します。

```powershell
cd C:\develop\FastDecisionServer
pwsh -NoProfile -File .\dev\scripts\setup.ps1 -Device cpu
.\.venv\Scripts\fds.exe download
pwsh -NoProfile -File .\dev\scripts\fds.ps1 -Command start -Device cpu
```

NVIDIA GPUを使う場合は、導入時と起動時の `-Device cpu` を `-Device cuda` に変更します。必要な環境と設定は[開発者ガイド](docs/DEVELOPERS_GUIDE.md)を参照してください。

待受はこのPCの [127.0.0.1:8767](http://127.0.0.1:8767/health) です。`ready: true` はローカルモデル常駐またはクラウド認証設定済みを示します。クラウドの実接続確認は判定要求で行います。

```powershell
pwsh -NoProfile -File .\dev\scripts\fds.ps1 -Command status
pwsh -NoProfile -File .\dev\scripts\fds.ps1 -Command stop
```

停止は現在の計算が終わるまで待ちます。詳しい運用は[利用ガイド](docs/USER_GUIDE.md)にまとめています。

## Cloudflare Clef

クラウド接続にはアカウントIDとWorkers AI用APIトークンを設定します。[設定・起動・接続確認](docs/CLOUDFLARE.md)を参照してください。Jeffの既定動作は変わりません。

## 判定例

```powershell
$body = @{
    model='jeff-qwen-2b'; device='cpu'; state='猫は動物です。'
    questions=@{ animal=@{ type='noul'; instructions='猫は動物ですか？' } }
    timeout_seconds=30; priority='interactive'
} | ConvertTo-Json -Depth 8
Invoke-RestMethod http://127.0.0.1:8767/v1/decisions -Method Post -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body))
```

`noul` は真偽の確率、`choice` は候補ごとの確率、`score` は尺度から得た値を返します。確率はモデルの推定であり、操作の実行権限や承認を与えるものではありません。[API仕様](docs/API.md)と、起動中の[対話API仕様](http://127.0.0.1:8767/docs)で入力・応答を確認できます。

## モデルとCPU・GPU

必要なメモリと常駐枠があれば、同じモデルのCPU用・GPU用を同時に保持できます。切替先が常駐済みなら再利用します。既定は6組まで保持し、自動アンロードは無効です。容量不足時は既存モデルを保持して追加ロードを拒否します。設定と確認方法は[利用ガイド](docs/USER_GUIDE.md)を参照してください。

## 資料

| 目的 | 参照先 |
|---|---|
| 起動・停止、モデル管理、速度の確認 | [利用ガイド](docs/USER_GUIDE.md) |
| 判定・管理APIの入力、応答、制限 | [API仕様](docs/API.md) |
| 環境構築、設定、モデル追加、試験方法 | [開発者ガイド](docs/DEVELOPERS_GUIDE.md) |
| 開発経緯、版ごとの実装・測定記録 | [開発資料](dev/README.md) |
| 開発時の作業ルールと資料の配置 | [AGENTS.md](AGENTS.md) |

ソース: [FastDecisionServer](https://github.com/KotorinChunChun/FastDecisionServer)
