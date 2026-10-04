# 配布規則

FastDecisionServer（fds）は独立したPythonサーバー/CLI。初版はソース・固定Jeff submodule・専用venv作成スクリプトから再現する。DesktopAgentのMSIXとは別に管理し、利用者の指示なしにMSIX化・公開・インストールしない。

- 版は `pyproject.toml`、`src/fds/__init__.py`、FastAPI/healthの値を揃える。
- compileall、unittest、依存整合性を確認し、実機測定の範囲と未測定を記録する。
- `.venv`、`models`、`runtime`、`config.local.json`、秘密情報、試験出力をGit・公開成果物から除外する。
- Jeffとモデルのライセンスを保持し、再配布時に対象ライセンス条件を確認する。現作業ではモデルを外部配布しない。
- GitHubはprivate。外部への公開は明示的な依頼が必要。
- 正常終了は `fds stop`。計算の実終了を待ち、自動的に強制終了へ切り替えない。