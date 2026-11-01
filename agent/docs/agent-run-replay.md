# Run の実行構成の指紋と記録再生（#67）

業務 Agent・スキルの版だけでは、モデル・MCP の schema・指示の組み立てが変わった後の回帰を、業務 Agent の変更か実行環境の変更かに切り分けられない。そこで、Run が実際に使った構成を hash の参照で残し（**実行構成の指紋**）、合成の記録でモデルと MCP を呼ばずに組み込み Runtime の制御を再生する（**記録再生**）。

| 入口 | 目的 | 外部の呼び出し |
|---|---|---|
| `POST /api/runs/{run_id}/replay`（既存。画面の「再実行」） | 利用者が同じ質問で新しい Run を実際に実行する | モデル・MCP を実際に呼ぶ（書き込みも承認のうえで実行する） |
| `python -m app.cli.run_replay <記録>`（新規。開発・CI） | 記録したモデルの出力とツールの応答で制御を再生し、記録の結果と比べる | 0（実の通信は遮断して数える） |

既存の再実行 API の意味は変えていない。記録再生は SDK の承認の復元（`resume_run`）・障害の後の再開とも別の契約である。

## 実行構成の指紋（`metadata.run_config`）

組み込み Runtime が SDK の Agent を組み立てるたびに（承認の後の再開を含む）、Run の metadata に残す（予約の key。利用者の値で上書きできない）。本文・資格情報は含めない。

| key | 内容 |
|---|---|
| `fingerprint` | 下の 6 つの部分の sha256 の先頭 16 桁 |
| `instructions.static_sha256` | 指示の変わらない部分（業務 Agent の指示・スキル・データの範囲の案内。Run の状態を足す前） |
| `tools` | モデルに渡すツールの定義（名前・説明・引数の schema）の sha256 と名前 |
| `skills[]` | 実行が使うスキル（公開の版に固定した中身）の定義の sha256 と、参照文書の Run の開始の時点の本文の sha256 |
| `mcp.<接続>` | MCP 接続の `tools/list` から作ったツールの schema（入力・出力・副作用）の sha256 と名前 |
| `model` | モデルの ID・推論の量・出力 / 入力の上限・画像対応・`snapshot: "unverified"` |
| `data_scopes.sha256` | 業務 Agent のデータの範囲（RAG / NL2SQL のプロファイル）の sha256 |
| `sdk_version` | OpenAI Agents SDK の版（指紋には入れない） |
| `previous_fingerprints` | 同じ Run の中で作り直した構成が違ったときの前の指紋（承認待ちの間に MCP の schema が変わったなど） |

- **ツール権限の「承認」/「自動」は指紋に入らない。** モデルに渡すツールが変わらないので、記録したモデルの出力のまま再生でき、承認の壁の違いを再生で検出する。「拒否」はツールを渡さないので `tools` が変わる。
- 参照文書の本文は固定していない（#1855。読むのは今の本文）。`skill_reference_read` の結果に読んだ時点の本文の `sha256` を足した。`run_config.observed_references(steps, run_config)` が、Run の開始の時点の hash と違う・同じ Run の中で変わった参照を `changed: true` にする（その Run は同じ本文を読み続けた保証が無いので、比較に使わない）。hash を記録しても本文は復元できない。
- RAG の検索対象の版は Agent が持たない。`run_config.observed_rag_targets(steps)` が、RAG のツールの結果が公開の契約で返す回答の provenance（検索・回答プロファイルの ID・設定の sha256・更新時刻・プロンプトの版）と根拠の `chunk_set_id` を拾う。RAG の文書・索引の版の記録は RAG（#59 の評価の manifest）が持つ。
- モデルの重みの版は provider が公開しないので、同じモデルの ID の中身の変更は指紋で検出できない。
- 保存期間と閲覧の権限は Run と同じ（Run を消せば消える。見られるのは Run を見られる人＝持ち主と system_admin）。hash だけなので、Run の本文より広い範囲に出す情報は無い。

## 記録再生

```bash
cd agent/backend
R=../evaluation/replay/approval-wall.v1.json
uv run python -m app.cli.run_replay $R                                   # 記録どおりか
uv run python -m app.cli.run_replay $R --policy ops__register_item=allow # 承認の壁を外す
uv run python -m app.cli.run_replay $R --policy ops__lookup_item=ask     # 承認の壁を足す
uv run python -m app.cli.run_replay $R --policy ops__register_item=deny  # ツールを渡さない
uv run python -m app.cli.run_replay $R --update                          # 期待を作り直す
uv run python -m app.cli.run_replay $R --json                            # 結果を JSON で
```

終了コード: `0` 構成も結果も記録と同じ、`1` 構成は同じで結果が違う（制御の回帰・承認の壁の違い）、`2` 比べられない（記録の誤り・構成の違い・記録との不一致・機微情報・実の通信の試み）。

### 記録（JSON）

同梱の例: [`agent/evaluation/replay/approval-wall.v1.json`](../evaluation/replay/approval-wall.v1.json)（合成。照会 → 書き込み → 承認 → 完了）。

| key | 内容 |
|---|---|
| `agent` | 業務 Agent の名前・指示とスキル（`id`・指示・`mcp_requirements`・参照文書の本文） |
| `connections[]` | MCP 接続（`server_id`・ツールの名前・説明・`input_schema`・`read_only`）。`tools/list` の応答になる |
| `model` | 記録したときのモデルの設定（ID・推論の量・上限。接続先・資格情報は持たない） |
| `tool_policy` | ツール権限（`default_mode`・`allow` / `ask` / `deny`）。`--policy` で上書きできる |
| `goal` | 利用者の質問 |
| `model_turns[]` | モデルの呼び出しごとの出力（`function_call` の名前・引数・`call_id`、`message` の文） |
| `tool_results[]` | `tools/call` の応答（`server_id`・ツール・引数・MCP の result）。引数を省くと引数を問わない |
| `approvals[]` | 承認待ちになったときの判断（ツールの名前・承認 / 却下） |
| `expected` | `--update` が書く: `run_config`（構成）と `outcome`（結果） |

`outcome` は Run の状態・ツールごとの判定（`decision`）と状態・承認・**呼び先へ届いた `tools/call`（`delivered`。実走なら副作用が起きる呼び出し）**・成果物の種類・回答。

### 境界（fail closed）

- モデル: 記録の出力を順に返す（`agents.testing.ScriptedModel`）。記録より多く呼べば「比べられない」。
- MCP: 記録の接続の `initialize` / `tools/list` / `tools/call` だけを `httpx.MockTransport` で返す。記録に無い呼び出し・メソッドは呼び先へ送らずに失敗にし、「比べられない」。書き込みのツールも記録の応答を返すだけで、副作用は起きない（再送もしない）。
- 実の通信: `httpx` の同期・非同期の transport を遮断して数える（`external_calls.live`）。1 件でもあれば「比べられない」。
- 記録の構成: 今の構成の指紋の部分が `expected.run_config` と違えば「比べられない」（`tools（消えた: …）` のように部分の名前を出す）。
- 使われなかった記録（モデルの出力・ツールの応答・承認の判断）は、制御の違いで Run が先に止まった・別の道を通った印として「結果の違い」に数える。
- 機微情報: 記録に資格情報・個人情報らしい値（Bearer・URL の資格情報・機微な名前の文字の項目・メール・カード番号など。ツールの出力の検査と同じ規則）があれば読まない。再生の後、保存した Run（失敗の経路を含む）に同じ形の値が残っていれば「比べられない」。
- process: CLI は `.env` を読まず、保存先を memory にする（DB・保存先に書かない）。

### 再生で検証できる範囲と、できない範囲

| 再生で検証できる（制御） | 新しい実走の評価が要る |
|---|---|
| ツール権限（自動 / 承認）と承認の判断の扱い、承認待ちで止まること、却下したツールを実行しないこと | 指示・スキル・ツールの説明・schema・モデルを変えた後の回答の品質（記録したモデルの出力は前の構成に対する出力） |
| 呼び先へ届く呼び出し（副作用）の有無と順序 | 呼び先（RAG / NL2SQL / 外部 MCP）の結果の変化（記録の応答は固定） |
| 予算・ツールの呼び出しの上限・回答の検証など、Runtime のコードの判定 | モデルの揺れ（seed は使わない）・provider の中身の変更 |

指紋が違う記録を再生しない理由: 記録したモデルの出力を今の構成に当てはめると、「今のモデルなら呼ばなかったツール」を呼んだことになり、制御の検証として誤った安心を与える。

### 記録の作り方と扱い

- 記録は合成のデータで手で書く（業務の Run を自動で記録に変換する機能は持たない）。業務の Run の原文・secret を記録・公開の fixture・CI の artifact に流用しない。
- 期待は `--update` で書く（使われなかった記録・記録との不一致があれば書かない）。構成を意図して変えたら（ツールの説明・共通の指示の変更など）、同じ PR で `--update` し直し、差分（`expected.run_config`）を review する。
