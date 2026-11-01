# 日本語の合成 PDF とテスト質問

RAG と業務 Agent の検索・回答を確かめるための合成資料です。PDF の本文、表、条件、確認番号はすべて架空で、実際の業務や運用条件を示しません。

10 件の PDF は合計 110 ページです。グループ A・B はそれぞれ 5 件、55 ページで、各グループの質問は 15 件です。

## ファイル

| グループ | PDF | 形式 | ページ数 |
|---|---|---|---:|
| A | [テスト_01_運用手順書.pdf](テスト_01_運用手順書.pdf) | 手順書 | 5 |
| A | [テスト_02_操作FAQ.pdf](テスト_02_操作FAQ.pdf) | FAQ | 8 |
| A | [テスト_03_月次稼働報告.pdf](テスト_03_月次稼働報告.pdf) | 表形式の報告 | 10 |
| A | [テスト_04_障害対応フロー.pdf](テスト_04_障害対応フロー.pdf) | 手順とフロー図 | 12 |
| A | [テスト_05_設備保守台帳.pdf](テスト_05_設備保守台帳.pdf) | 台帳 | 20 |
| B | [テスト_06_文書登録ガイド.pdf](テスト_06_文書登録ガイド.pdf) | 手順書 | 5 |
| B | [テスト_07_検索FAQ.pdf](テスト_07_検索FAQ.pdf) | FAQ | 8 |
| B | [テスト_08_品質評価報告.pdf](テスト_08_品質評価報告.pdf) | 表形式の報告 | 10 |
| B | [テスト_09_公開承認フロー.pdf](テスト_09_公開承認フロー.pdf) | 手順とフロー図 | 12 |
| B | [テスト_10_資料仕様一覧.pdf](テスト_10_資料仕様一覧.pdf) | 仕様一覧 | 20 |

- [questions.md](questions.md)：日本語の質問 30 件と期待する回答、PDF のページへのリンク。
- [questions.json](questions.json)：同じ質問の構造化データ。回答の要点、判断、出典と引用断片を含みます。
- [manifest.json](manifest.json)：PDF の名前、グループ、形式、ページ数、SHA-256。
- [resources.json](resources.json)：論理キー A / B ごとの KB・検索・回答プロファイルの定義と文書レシピ。環境固有の ID・URL は含みません。
- [agent/examples/test-pdfs-ja/agents.json](../../../agent/examples/test-pdfs-ja/agents.json)：論理キー A / B ごとの業務 Agent の定義（Agent の側に置きます。RAG は Agent を呼ばないため。#3545）。

## 管理 API と Agent の命令による再構築

2 段で作ります。先に RAG の命令で KB・検索・回答プロファイル・文書を作って取り込み、次に Agent の命令で、RAG の命令が残した検索・回答プロファイルの ID を使って業務 Agent を作成・公開します。RAG の命令は RAG の管理 API だけ、Agent の命令は Agent の管理 API だけを呼びます（2 つは `--state` のファイルだけでつながります）。

前提は RAG と Agent の配備、管理 API を操作できる利用者、利用可能な Oracle / Object Storage、Docling の解析サービス、embedding・rerank と OCI Enterprise AI の接続です。Agent には組み込みの `business-rag-research` スキルと RAG の MCP 接続が必要です。モデルや接続先は対象環境のシステム設定を使います。取込の自動処理は各文書のレシピで選び、全体の設定・ツール権限は変更しません。

### 1. RAG: KB・検索・回答プロファイル・文書

repo の直下で実行します。URL は RAG の `/api` までを指定します。下のホスト名は説明用です。ログイン ID とパスワードは対話で入力し、Cookie はプロセスのメモリだけに保持します。自動実行では `SUITE_TEST_LOGIN_USER` / `SUITE_TEST_PASSWORD` を secret store から環境変数として渡せます。引数・構成ファイルへパスワードを書かないでください。

```bash
uv run --project rag/backend python -m app.rag.test_resources_cli \
  --fixture-dir rag/examples/test-pdfs-ja \
  --rag-api https://suite.example.invalid/rag/api \
  --state tmp/test-resources/state.json
```

私設 CA を使う環境では `--ca-file <CA の PEM ファイル>` を追加します。証明書の検証は無効にしません。

1. 変更前に 10 PDF の SHA-256 とページ数を確認します。
2. KB・プロファイル・文書は名前と定義で照合します。文書は同じ原本・所属・レシピの場合だけ再利用します。既存の別の文書や設定を上書き・削除しません。
3. 作成時の ID で所属と参照を解決します。文書は Docling・親子階層の分割で取り込み、全件の `INDEXED`・検索可能・active chunk set・chunk 数と vector 数の一致を待ちます。
4. 環境固有の ID（グループごとの KB、検索・回答プロファイルの `profile_ids` と既定の `default_profile_id`）、文書ごとの索引・ベクトル数、名前の区分、全体の完了状態を `--state` に保存します。認証情報や接続先は保存しません。このファイルは commit しません。

同じ命令を再実行すると既存対象を再確認します。作成の応答を失った場合も API の一覧から再発見し、進行中の取込を二重投入しません。中断やタイムアウトの後も同じ命令で再開できます。同じ対象への命令は同時に 2 つ起動しないでください。同名の対象が複数ある場合、定義・原本・所属が異なる場合、失敗・取消の取込は理由を示して終了コード 1 で停止します。自動工程の途中にも REVIEW が現れるため、確認待ちは索引完了まで待ち、上限を超えたら未完了として停止します。取込自体は自動で取り消しません。対象の画面で原因を直し、必要なら再処理してから再実行します。待機上限は `--timeout`、確認間隔は `--poll-interval`（秒）で指定できます。

既存の一式を使わず別のテスト一式を作るには `--namespace <英数字等の区分>` を追加します。KB・プロファイル・アップロード時のファイル名を `テスト_<区分>_…` とし、PDF の内容・checksum は変えません。区分は state に残り、次の Agent の命令が業務 Agent の名前に同じ区分を付けます。論理キー A / B と各ファイルの実 ID は state で確認できます。

終了コード 0 と state の `complete: true` が RAG の段の完了です。

### 2. Agent: 業務 Agent の作成・公開

repo の直下から、Agent の管理 API（`/api` までの URL）を呼びます。ログインは 1 と同じく対話（自動実行では `SUITE_TEST_LOGIN_USER` / `SUITE_TEST_PASSWORD`）で、組織の業務 Agent を作るため Agent 管理（`agent.admin`）か system_admin の利用者を使います。1 の state を渡します。

```bash
uv run --project agent/backend python -m app.cli.agent_test_resources \
  --agent-api https://suite.example.invalid/agent/api \
  --rag-state tmp/test-resources/state.json \
  --state tmp/test-resources/agent-state.json
```

state の代わりに `--profile A=<プロファイルの ID> --profile B=<プロファイルの ID>`（必要なら `--namespace <区分>`）でも渡せます。定義は既定で [agents.json](../../../agent/examples/test-pdfs-ja/agents.json) を読みます（`--definitions` で変えられます）。私設 CA を使う環境では `--ca-file` を追加します。

1. state の `complete: true` を確かめ、グループごとの業務 Agent のデータの範囲（`data_scopes.rag`）に、対応するプロファイル 1 件だけと既定を設定します。
2. 業務 Agent は名前で照合します。無ければ組織の業務 Agent として作ります。あれば定義と違う項目だけ下書きを直します（プロファイルの ID が変わったときなど）。同名が複数ある、組み込み・個人の業務 Agent、移行が要る状態のときは変更せずに終了コード 1 で停止します。
3. 公開していない変更があるときだけ公開し、公開中の版の指示・スキル・データの範囲を確かめます。
4. 業務 Agent の ID と公開中の版を 1 行の JSON で出し、`--state` に保存します（このファイルも commit しません）。

作成・変更・公開は画面と同じ API を通るので、画面と同じ検証を受けます。データの範囲のプロファイルを、実行した利用者が使えるか（Agent の backend が RAG の MCP で確かめます）と、公開前の評価の門です。断られた場合は理由を示して終了コード 1 で停止します（公開を断られたときは下書きが残ります）。同じ命令を再実行しても、変更が無ければ読むだけです。

2 つの命令が終了コード 0 で終われば構築完了です。これは検索回答の正答率を表しません。構築後は次の質問集を使い、事実・引用・検索範囲の分離を別に確認します。Agent のツール呼び出しが承認待ちになったら、画面で各呼び出しを確認して承認します。検索が実行されていない保留を正答として数えたり、全体のツール権限を自動変更したりしません。

## 質問の使い方

1. A・B のナレッジベースを作り、各グループの PDF を 5 件ずつ登録します。
2. 文書の処理を始め、全件の embedding と索引構築が完了し、検索可能であることを確認します。
3. 検索・回答プロファイルをグループごとに作り、それぞれ対応する KB 1 件だけを参照させます。
4. Agent で試す場合は `business-rag-research` スキルを選び、`data_scopes.rag.profile_ids` と `default_profile_id` に対応するプロファイルだけを設定して公開します（上の「2. Agent」の命令が行います）。
5. A の質問は A のプロファイル／Agent、B の質問は B のプロファイル／Agent に入力します。各質問は新しい会話で実行し、期待する回答は入力に含めません。

条件検索、表の数値・計算、フローの分岐、操作の完了判定、例外・否定条件、文書間の照合を含みます。`test-a-15` は資料に個人名・メールアドレスがない場合、`test-b-15` は別グループの情報を求めた場合の確認です。この 2 件は回答の保留を期待し、氏名・連絡先・日数を推測して補わないことを確認します。両グループをまとめて検索すると、検索範囲の分離を検証できません。

期待する回答は意味を照合するための基準です。空白、全角・半角、曜日や時刻の表記が違っても、事実・単位・条件が一致すれば許容します。数値だけが一致していても、章・期間・項目や対象グループを取り違えた回答は正解にしません。

`sources` は期待する回答を裏付ける出典です。ページは先頭を 1 とする物理ページで、`evidence_quotes` はそのページの本文・表から抜き出した断片です。PDF の改行や空白は正規化して照合します。通常の質問では回答の要点を裏付ける引用を確認し、文書間の照合を求める `test-b-14` は両方の文書を確認します。資料不足の質問は `sources` を空にしています。

## JSON の扱い

`questions.json` は環境に依存しない質問集で、評価 API にそのまま渡す request ではありません。実環境の ID は含みません。

| 項目 | 意味 |
|---|---|
| `id` | 質問を識別する論理キー |
| `group` | 実行先のグループ A / B |
| `primary_document` | 質問の主な対象。各 PDF に 3 件ずつ割り当て |
| `type` | 確認する検索・回答の性質 |
| `question` | 入力する日本語の質問 |
| `answerable` | 指定したグループの資料で回答できるか |
| `expected_response_decision` | 期待する判断。`answer` 28 件、`hold` 2 件 |
| `expected_answer` | 期待する回答 |
| `required_answer_points` | 回答で確認する必須の要点 |
| `sources` | PDF の名前、ページ、根拠となる引用断片 |

既存の評価形式へ変換する場合は、`question` を質問欄に、`expected_answer` を標準回答欄に設定します。ファイル名はアップロード後の文書 ID に解決し、A・B に分けて対応するプロファイルまたは Agent で実行してください。RAG の評価形式は [evaluation/README.md](../../evaluation/README.md) を参照してください。

構成定義と再構築コマンドの仕様は [#2905](https://github.com/engchina/no.1-production-ready-assistant/issues/2905)、業務 Agent の段を Agent の命令へ分けた経緯は [#3545](https://github.com/engchina/no.1-production-ready-assistant/issues/3545) を参照してください。

## 原本の確認

このディレクトリで次を実行すると、PDF の SHA-256 を確認できます。Python の標準ライブラリだけを使います。

```bash
python3 - <<'PY'
import hashlib
import json
from pathlib import Path

root = Path.cwd()
manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
for document in manifest["documents"]:
    path = root / document["file"]
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != document["sha256"]:
        raise SystemExit(f"原本が一致しません: {path.name}")
print("PDF 10 件の SHA-256 が一致しました。")
PY
```
