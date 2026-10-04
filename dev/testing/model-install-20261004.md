# 登録済み3モデルの導入記録（2026-10-04）

## 依頼と変更

ユーザーの「fdsに3つとも導入してください」に基づき、FDS専用の仮想環境から固定revisionのQwen 0.8B・Gemma E2Bを取得した。既導入のQwen 2Bは再取得せず保持。導入先はC:/develop/FastDecisionServer/models/、キャッシュは同プロジェクトの.cache/huggingface/とした。設定・ソース・モデル管理APIの方針は変更していない。サーバーAPI経由の未導入モデル取得を許可した変更ではない。

```powershell
cd C:\develop\FastDecisionServer
$env:HF_HOME='C:\develop\FastDecisionServer\.cache\huggingface'
.\.venv\Scripts\fds.exe download --model jeff-qwen-0.8b
.\.venv\Scripts\fds.exe download --model jeff-gemma-e2b
```

| モデル | 固定revision | ファイル合計MB |
|---|---|---:|
| Qwen 0.8B | 51552f39a943d62e8387c4ea5e17026a1408729f | 1726.60 |
| Qwen 2B | 00448e884cce9e34396d8c72e9a692b41383b1ee | 4447.64 |
| Gemma E2B | e3de3e99a979f92afd995d4e526c7e3170ae49cf | 9290.26 |

容量はモデルフォルダ直下のファイル合計を十進MBで表示。ダウンロードキャッシュは含まない。重みはGit対象外。

## 検証

- 2モデルとも公式huggingface_hubのsnapshot_downloadが正常終了し、.fds-revisionが設定と一致した。
- 稼働8767のGET /modelsで3モデルすべてinstalled=true・loadable=true、devices=auto/cpu/cudaを確認した。CPU/GPU選択可能の意味であり、3モデルのGPU同時常駐保証ではない。
- Qwen 0.8B・GemmaはFDS専用venvの独立プロセスでJeffBackend.decideを使用し、CPUロードと日本語判定に成功。試験プロセスの終了で試験用モデルを解放した。
- 既存Qwen 2Bは稼働FDSのPOST /v1/decisions、device=cpu、auto_unload=falseで確認した。
- 入力は「猫は動物です。」、質問は「猫は動物ですか？」。返却した確率は順に0.994657（0.8B）、0.994757（2B）、0.997301（Gemma）。ロード/応答の動作確認であり、品質・速度の比較試験ではない。
- 試験中に稼働FDSの常駐モデルを解放していない。終了時もpid=55540、ready=true、generation=2、Qwen2BのCPU/GPU常駐を確認。再起動なしで一覧へ反映した。
- TestJeffの開いている問い合わせ速度比較を再読み込みし、3モデルから「計測不能」が消え、選択可能になったことを確認した。A/Bと4モデルの選択は保持した。対戦の実行・利用者DBへの試験結果保存は行っていない。

生の検証結果はGit対象外のdev/testing/output/install-qwen08-20261004.json、install-qwen2-20261004.json、install-gemma-20261004.json、install-models-20261004.jsonに保存した。画面証跡はTestJeff/dev/testing/output/fds-three-models-installed.jpg。

## 運用

3モデルとも必要時にロードできる。既存の解放承認は維持され、空き容量不足や常駐上限による解放が必要な場合はクライアントの確認後に切り替える。今回の追加モデル検証はCPUであり、FDSでの3モデルGPU切替の実測とは区別する。

## 後続の運用変更（2026-10-05の文書整理）

上記「運用」は導入確認時点のv0.2.0の方針。後続の[v0.2.2](../history/v0.2.2/v0.2.2-imp.md)では既定上限6組・自動解放禁止となり、不足時は既存モデルを保持して追加ロードを拒否する。現在の利用方法は[利用ガイド](../../docs/USER_GUIDE.md)を参照する。本書のモデル導入・CPU判定の結果は変更せず、今回の文書整理で再測定はしていない。
