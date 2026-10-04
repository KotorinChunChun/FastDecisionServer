# FastDecisionServer 開発ルール

- FastDecisionServer の短縮名は **fds**。DesktopAgent 以外からも利用する独立した共用判定サーバーである。
- 質問には回答だけを行い、明確な変更指示がなければ変更しない。PowerShell は `pwsh`、文書・コミットは日本語とする。
- 本書と目的別目録から必要な資料だけを読む。`dev/private/`、`dev/temp/`、`runtime/`、利用者データは通常調査・静的検査から除外する。
- 独立した `.venv/` と `models/` を使う。TestJeff のモデル・環境・プロセスを変更しない。モデル重み・入力画像・要求本文を Git へ保存しない。
- 推論は単一ワーカーで実行し、要求ごとのモデル・device を全質問で固定する。期限・切断による応答取消後も実際の計算が終わるまで推論枠を保持する。
- 初版は loopback のみで待ち受ける。管理停止は起動ごとのトークンで認証する。ネットワーク公開は明示依頼を必要とする。
- 大きな実装はフェーズに分け、適切な試験後に `feat:`・`fix:`・`docs:` と日本語要約でコミットする。個人開発の既定ブランチは main。
- `README.md` は利用者の入口、`docs/` は公開開発者向け仕様、`dev/` は要求・実装計画・検証記録。正式資料は Git で追跡する。
- 実装中の `dev/vX.Y.Z-req.md` と `dev/vX.Y.Z-imp.md` は dev 直下に置く。完成した一式だけ `dev/history/vX.Y.Z/` へ移す。未完了の出典と引継ぎ先を記録し、次版開始だけで完了としない。
- `dev/scripts/` は再現用スクリプト、`dev/testing/` は測定記録、`release/` は Git 対象外の配布成果物。`dev/temp/` はプロジェクト外の一時制作物、`dev/private/` は秘密情報用として区別し、いずれも Git と通常調査から除外する。
- default テンプレートの必要事項は本書へ統合済み。取り込み用 templates は保存しない。default 原本を変更しない。
- 新規プロジェクトの登録先は `C:/develop/default/dev/アプリ一覧.md` と `C:/develop/DevLauncher/gui/CLI-Launcher.Gui/web-catalog.json`。手順は `C:/develop/DevLauncher/docs/APP_CATALOG.md`。本作業では親担当が登録する。
- 新規 GitHub リポジトリは private とする。配布物生成・公開・インストールを区別する。

## 目的別目録

| 目的 | 資料 |
|---|---|
| 利用方法 | [README](README.md) |
| 構築・API・モデル追加 | [開発者ガイド](docs/DEVELOPERS_GUIDE.md) |
| 現版の要求・実装 | [要求](dev/history/v0.1.0/v0.1.0-req.md)、[実装](dev/history/v0.1.0/v0.1.0-imp.md) |
| 測定・検証 | [検証結果](dev/testing/v0.1.0-results.md) |
| 配布方針 | [配布規則](dev/RELEASE_RULE.md) |
モデル常駐管理・解放承認・CPU/GPU切替: [実装と統合確認](dev/history/v0.2.0/v0.2.0-imp.md)、[API](docs/DEVELOPERS_GUIDE.md)。

登録済み3モデルの導入・日本語判定確認（2026-10-04）: [導入記録](dev/testing/model-install-20261004.md)。
