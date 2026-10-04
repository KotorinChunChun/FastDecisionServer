# 配布規則

FastDecisionServer（fds）は独立したPythonサーバー/CLI。ソース・固定Jeff submodule・専用venv作成スクリプトから再現する。DesktopAgentのMSIXとは別に管理し、利用者の指示なしにMSIX化・公開・インストールしない。

## 変更の検証

- 版は `pyproject.toml`、`src/fds/__init__.py`、FastAPI/healthの値を揃える。文書だけの整理では版を増やさない。
- 本体変更ではcompileall、unittest、依存整合性を確認する。必要な実機測定は模擬試験と区別し、日時・条件・未測定範囲をdevへ記録する。実行方法は[開発者ガイド](../docs/DEVELOPERS_GUIDE.md)を参照する。
- 文書だけの変更では現行仕様との照合、相対リンク・差分の検査を行う。本体の再測定やサーバー再起動を実施したとは記録しない。

## 保存・配布の対象

- ソース、試験、構築スクリプト、公開可能な説明と開発記録をGitで追跡する。
- `.venv/`、`.cache/`、`models/`、`runtime/`、`config.local.json`、`dev/private/`、`dev/temp/`、`dev/testing/output/`、`release/` はGitから除外する。秘密情報・利用者入力・承認tokenを公開成果物へ含めない。
- 秘密資料への参照許可はGit登録・公開許可を意味しない。
- Jeffとモデルのライセンスを保持する。モデル重みを通常のGit配布対象へ含めず、別途再配布する場合は対象ライセンス条件を確認する。
- GitHubはprivate。配布物の生成、Gitへの保存、外部公開、インストールを区別する。外部公開は明示的な依頼を必要とする。

## サーバーの終了

正常終了は `fds stop`。計算の実終了を待ち、自動的に強制終了へ切り替えない。起動・停止の手順は[利用ガイド](../docs/USER_GUIDE.md)を参照する。
