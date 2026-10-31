<!--
記述規約の正本は docs/agents/github-workflow.md の「GitHub Issue / Pull Request の記述規約」（要約は AGENTS.md の「Issue / PR の書き方」）。
- PR title は `<type>(<scope>): <日本語の要約> (#<issue-number>)`。
  type は feat / fix / docs / test / refactor / chore / ci など。scope は rag / nl2sql / agent / platform
  （複数にまたがる場合や monorepo 全体は省略可）。
- merge は PR の最新 commit の `CI OK` が成功してから行う（判定は `scripts/pr-merge-check.sh <PR>`）。
- PR 作成後に追加修正や検証結果の変化があった場合は、コメントだけで済ませず本文を最終状態へ更新する。
不要な HTML コメントは削除して構いません。
-->

## 関連 Issue

Closes #<issue-number>

<!-- merge で完了する Issue は `Closes #N`、参照のみは `Refs #N`。複数ある場合はすべて列挙する。 -->

## 背景 / 原因

Issue の要点と、この変更が必要な理由を記載する。bug fix では根因を記載する。

## 変更内容

- 変更した責務・挙動を具体的に記載する
- schema / API / UI / data migration / compatibility への影響を記載する

<!-- commit の羅列ではなく、reviewer が挙動差分と責務境界を判断できる粒度で書く。
     変更していない重要範囲や backward compatibility も必要に応じて明記する。 -->

## 検証結果

- `<実行した command>` — pass / fail / skip と件数
- 手動確認または Playwright の対象 flow / viewport / 状態

<!--
- 実行した正確な command と結果を書く。失敗・skip・未実行を隠さず、今回の変更によるものか既存問題かを分ける。
  実行できない test がある場合は理由と代替確認を書く。
- command は各製品の AGENTS.md の開発コマンドを基本とする。
  backend: `uv run pytest` / `uv run ruff format --check .` / `uv run ruff check .` / `uv run mypy .`
  frontend: `npm run lint` / `npm run build` / `npm run test`
- ローカルで実行した command と、CI の job 結果（全件の検査）を分けて書く。ローカルで実行しなかった全件の検査
  （backend の全テスト・mypy・pip-audit・e2e の smoke 等）は、CI の job 名と pass / fail を引用してよい。
- platform/ を変更した場合は、影響を受ける製品の検証結果（統合 CI の該当 job を含む）も書く。
- UI/UX 変更では、対象 Playwright spec、desktop / 375px viewport、主要導線と
  空 / 読込 / エラー / ブロック状態の結果を書く。見た目を変えた場合は必要に応じて screenshot または
  visual check の結果を添え、意図的な見た目の変更は design-system README §7 と照合した結果を書く。
- OCI / Oracle / LLM を呼ぶ範囲は、CI 上の決定論スタブ確認と手動 / ステージングでの
  実サービス確認を区別して書く。
- docs-only など test 対象外でも省略せず、`git diff --check` 等の実施結果と、
  コード test を実行しない理由を書く。
-->

## 既知の制約・残課題

- 未対応範囲、既知の制約、follow-up Issue を記載する。なければ「なし」と記載する。
