# GitHub の運用と Issue / PR の書き方

> root の [AGENTS.md](../../AGENTS.md) から移した詳細（#2276）。root の規則と同じ強さで守る。規則を変えるときはこのファイルを直し、root には規則の要約と案内だけを書く。
> 正本: 見出しと各項目は [Issue のテンプレート](../../.github/ISSUE_TEMPLATE/issue.md)・[PR のテンプレート](../../.github/PULL_REQUEST_TEMPLATE.md)、merge の判定は `scripts/pr-merge-check.sh`。

## 開発ワークフロー / GitHub 運用

- **`main` へ直接 commit / push しない。** Issue を先に作り（label `product:rag` / `product:nl2sql` / `product:agent` / `platform`。複数ならすべて）、作業ブランチ `codex/<issue-number>-<short-topic>`（使えなければ Issue 番号と作業内容が分かる名前）で変更し、PR を出す。`platform/` と製品にまたがる変更は 1 つの PR でよい。
- **検証と PR 本文の更新が済み、PR の最新 commit の `CI OK` が成功したら、ユーザーの確認を求めず `gh pr merge <PR> --merge`（merge commit）で merge する。** PR の作成や CI の成功の報告だけで終わらない。
- **CI はエージェント自身が待ち、merge の可否は `scripts/pr-merge-check.sh <PR>` で判定する**（「pass したら声をかけて」と止まらない。待つ間は別の独立した作業をしてよい。実行環境の PR / CI の監視機能を使ってもよい）。スクリプトは最新 commit の CI の run を待って `CI OK` を確かめ、`ready-to-merge`（終了 0）/ `needs-update-branch`（3）/ `ci-failed`（1）を出す。merge はしない（`--update` で `gh pr update-branch` も行う、`--no-wait` は待たない）。`gh pr checks --watch --required` は使わない（CI の開始直後は `CI OK` がまだ無く、待たずに終わる）。
- **main の取り込みは必要なときだけ（#339・#1581。ruleset は up-to-date を求めない）。** PR の最新の CI の開始後に main で変わったファイルが、PR が変えたファイルと同じか、全体に効くファイル（直下・`platform/` の `package.json`・`package-lock.json`・`uv.lock`・`pyproject.toml`、製品の下のそれらは PR が同じディレクトリを触るときだけ（#1634）、`.github/workflows/`・`.github/actions/`、`playwright` / `vite` / `vitest` の config・`tsconfig*.json`、e2e の fixture、`platform/scripts/e2e_impact.py` など。一覧は `scripts/pr-merge-check.sh` の `GLOBAL_PATTERNS`）のときだけ、main を取り込んで（`gh pr update-branch <PR>` か `git merge origin/main`）CI をやり直す。重ならない変更はそのまま merge してよく、merge の後の main の push の CI（失敗すれば `ci-failure` の Issue）で確かめる。続けて merge するときは、前の merge の後にもう一度判定する。
- CI の失敗・merge conflict は直して再検証してから merge する。ruleset を迂回した強制 merge はしない。解消できなければ原因と未完了の操作を明示する。
- **main が赤になったら（`ci-failure-issue.yml` が label `ci-failure` の Issue を作る）、ほかの作業より先に直す。** 直す PR はその Issue を `Closes` する。
- **merge 後はローカルを `main` に切り替え、`git fetch --prune` のうえ `origin/main` へ fast-forward し、その作業の worktree とローカルのブランチを消してから完了を報告する**（PR の merge 状態・ブランチ・同期を確かめる）。未保存の変更を破棄する `reset --hard` 等は使わない。同期できなければ理由と残作業を明示する。
- docs-only や緊急の修正も同じ流れ。例外が要るなら理由を添えてユーザーに確認する。
- 並行する別のセッションの未コミット変更・stash・worktree を、確認なしに破棄・上書きしない。
- **使わなくなった worktree は消す（#2032）。** PR の merge・作業の中止の後、その作業の worktree を `git worktree remove <path>` で消し（各 worktree が `.venv`・`node_modules` を持ち、1 つで 1〜3GB を使う）、merge 済みのローカルのブランチを `git branch -d` で消す。未 commit の変更か push していない commit がある worktree は消さない。他の session・実行中の agent の worktree（`git worktree list` の locked）は、使われていないことを確かめてから消す。
- **開発の ADB が停止していたら、利用者に聞かずに起動する**（夜間に自動で停止することがある。#1825）。対象は `platform/.env` の `PLATFORM_ORACLE_DSN` の接続先の DB。実サービスの確認・長い評価の前と、DB に接続できない（`DPY-6005`・`ORA-12541`・画面の「データベースに接続できません」など）ときに状態を確かめ、`STOPPED` なら起動して `AVAILABLE` になるまで待ってから続ける（数分かかる。止まっていた間の評価・確認はやり直す）。
  - 画面・backend の API と配備のスクリプトは DB の起動・停止・状態の取得を持たない（#10）。OCI の ADB は OCI のコンソールか OCI CLI（`oci db autonomous-database get` / `oci db autonomous-database start --autonomous-database-id <ADB の OCID>`）、ADB Free コンテナは `docker ps -a` / `docker start <コンテナ名>` で確かめて起動する。
  - 停止（`stop`）は利用者の指示が無い限りしない。起動したことは作業の報告に書く。
- **一時ファイルは `/tmp` に置かず、repo の直下の `./tmp/` に置く**（開発環境の WSL がよく再起動し、`/tmp` が消えるため。#1616）。作業用の worktree は `.claude/worktrees/`、PR / Issue の本文・測定の結果・展開した binary などは `./tmp/<作業の名前>/`。`./tmp/` は `.gitignore` で無視し、commit しない。

## GitHub Issue / Pull Request の記述規約

見出しと各項目の書き方はテンプレート（[Issue](../../.github/ISSUE_TEMPLATE/issue.md)・[PR](../../.github/PULL_REQUEST_TEMPLATE.md)）にある。`gh issue create` / `gh pr create` で本文を渡すときも、テンプレートの見出しと注記どおりに書く。

- 日本語で書く。タイトルは対象と事象が分かる具体的な文にする（「修正」「対応」だけにしない）。
- 確認した事実と推測を分け、未確定は「調査中」「未確認」と書いて判明したら更新する。再現・レビューに要る具体情報（API・設定 key・status code・error message・再現値）を書き、secret・token・個人情報・実 credential は書かない。
- **業務の内容は書かない（repo は public。#2019）。** 利用者が扱う文書・データの業務の内容（顧客・案件・制度・文書・法令などの名前、ナレッジベース・プロファイル・業務 Agent などの利用者が付けた名前、質問や文書の実データの抜粋）を、Issue・PR・commit message・ブランチ名・コード・コメント・識別子・ログ・画面の文言・i18n・docs・テストの fixture のどこにも書かない。再現情報は技術の言葉と匿名の名前（「ナレッジベース A」「PDF 15 件」「条件の多い質問」）で書き、テストは合成の内容の fixture を使う。機能・エンジン・設定の名前も技術の言葉で付ける（業務の名前を付けない。例: 「XML ツリー JSON」）。利用者が会話で示した業務の文書・質問は、ローカルの確認（repo の外か `./tmp/`）だけに使う。
- **製品の不具合を確かめたら、必ず Issue を作る（#2488）。** 作業中（実装・検証・評価・配備・調査）に製品の不具合を確かめたら、ユーザーの指示を待たずに Issue を作る。
  - 対象: 再現した、またはコード・ログ・設定の根拠で確かめた不具合。推測だけの段階では作らず、確かめてから作る。依頼の範囲外の不具合、その場で直す不具合（直す PR は `Closes #N` で結ぶ）、修正の後に別の環境・経路で残っていた不具合（元の Issue が閉じていれば新しい Issue を作り `Refs #元` で結ぶ）も含む。同じ不具合の open の Issue があれば、新しく作らずにそこへ追記する。
  - 書き方: 上の規約のとおり匿名の技術の言葉で書く。業務の内容・secret・token・個人情報に加え、実環境の機密（IP アドレス・ホスト名・OCID・tenancy / compartment の名前・実データを含むログやスクリーンショット）を書かない。再現は合成の入力と技術の名前（「Compute A」「ナレッジベース A」）で示す。
  - 報告: 作った Issue の番号を、その作業の報告に書く。
- 製品の用語は各製品の `AGENTS.md` に従う（例: RAG の「DocRAG」は地の文にも識別子にも使わない）。
- **Issue** は `問題` / `症状` / `原因` / `修正方針` の 4 項目を必ず含める（原因が未確定でも仮説か「調査中」を書く）。`完了条件` は判定できる形で書く。
- **Issue の起票・追加調査は、後続の修正に使える根拠を残す。** タイトルや症状だけで判断せず、対象のコード経路・データの流れ、既存テスト、既存の診断記録と関連 Issue / PR の修正履歴を深く確認する。
  - 調査対象の commit、関係する file / function と、確認した事実の根拠を記載する。過去の記録と現在の実装を照合し、修正済みの部分・残る不具合・検証だけが未完了の部分を分ける。
  - 不具合の疑いがある箇所、原因の仮説、未確認事項とその切り分け方、責務境界に沿った最小の修正範囲、影響する経路と必要な回帰テストを整理し、対応する Issue の本文またはコメントへ追記する。既存の根拠や要件を失わず、推測だけの結論を確定した原因として書かない。
  - 既存テストのカバー範囲と不足を確認し、必要に応じて合成入力・決定論スタブで既存処理を確かめる。実行した command・結果と、静的確認・実サービス検証・未実施の範囲を区別して記録する。
  - **利用者が「調査のみ」「コード変更なし」を指定した場合は、製品コード・設定・データを変更しない。** 調査用の一時ファイルは既定の `./tmp/<作業の名前>/` に置く。修正案は Issue に残し、調査の依頼だけで実装・配備へ進まない。
- 規模の大きい機能の Issue は、仕様（spec）の形で書いてよい: 必須の 4 項目（`問題` / `症状` / `原因` / `修正方針`）の後に `ユーザーストーリー` / `実装の決定` / `テストの決定` と、テンプレートの任意の見出しの `完了条件` を足す。
- **PR title** は `<type>(<scope>): <日本語の要約> (#<issue-number>)`（`scope` は `rag` / `nl2sql` / `agent` / `platform`。複数・全体なら省略可）。本文は `関連 Issue`（`Closes #N` / `Refs #N`）/ `背景 / 原因` / `変更内容` / `検証結果` / `既知の制約・残課題`。
- `検証結果` は実行した command と結果を正確に書き、失敗・skip・未実行を隠さない。ローカルで実行しなかった全件の検査は CI の job の結果を引用してよい。`platform/` の変更は影響を受ける製品の結果、UI の変更は Playwright の結果、Oracle / LLM はスタブと実サービスの確認を分けて書く（詳細はテンプレート）。
- PR 作成後の追加修正・検証結果の変化は、本文を最終状態へ更新してから merge する。
