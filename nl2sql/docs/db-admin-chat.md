# DB 管理チャット（#2264）

「データ準備」の「管理 SQL を実行」の上の「DB 管理チャット」（`/db-admin-chat`）は、DBA の役割の利用者が自然言語の質問から Oracle の SQL（SELECT・DML・DDL・DCL・PL/SQL のすべて）を作り、確かめてから実行する画面。「チャット」（[sql-chat.md](./sql-chat.md)）は業務プロファイルの範囲の SELECT だけ、「管理 SQL を実行」は SQL を手で書く。DB 管理チャットはその間を埋める。

## 確定した仕様

| 項目 | 内容 |
|---|---|
| 権限 | メニュー権限 `menu.db_admin_chat`（「DB 管理チャット」。含む権限は DB 構造の参照・再取得）。既定はどのロールにも付けない（system_admin だけが使える）。ほかのロールへの付与は「自分の持つ権限だけを付与できる」規則（[agents/security-contracts.md](./agents/security-contracts.md)「role の権限昇格の防止」）で越権を防ぐ。「管理 SQL を実行」の権限（`menu.admin_sql`）では代わりにしない |
| 生成 | OCI Enterprise AI Direct だけ（システム設定 › モデルの既定のテキストモデル）。業務プロファイルで限らず DB 全体が対象。**質問には必ず SQL で答える**（#2333）: 構造化出力（`DbAdminChatSqlOutput`）の `sql` に 1 文以上、`explanation` は SQL が何をするかの短い説明。SQL の無い応答（文章だけの答え・文の種類が分からない文だけ）は、理由を添えて 1 回だけ作り直させ、それでも無ければ生成の失敗（502「SQL を生成できませんでした…」。ターンは保存せず、画面は質問を残して「再送信」）にする。文章を回答として出さない。方針で止める作業を頼まれたときも、今の設定・状態を読む SELECT を作る |
| 生成の文脈 | DB 構造の cache から質問（と画面の中の前の質問）に関係する表を最大 12 件選び、列（1 表 40 列まで）を渡す。ほかの表は名前だけ（80 件まで）。Oracle のシステムの schema と製品の表は cache の段階で外れている。DB の管理の質問（版・表領域・セッション・ロック・権限・無効な object など）は、LLM への指示（`backend/app/features/nl2sql/prompts/db_admin_chat/system.txt`）にデータ辞書と動的性能ビュー（`V$VERSION`・`PRODUCT_COMPONENT_VERSION`・`DBA_TABLESPACE_USAGE_METRICS`・`DBA_DATA_FILES`・`DBA_FREE_SPACE`・`V$SESSION`・`V$LOCKED_OBJECT`・`DBA_OBJECTS`・`DBA_ROLE_PRIVS` など）の使い方を書いて答えられるようにする。Autonomous AI Database で見えないビュー（`ORA-00942` / `ORA-01031`）は、エラーの分析で `USER_` / `ALL_` ビューの代わりを示す。生成した文の列は、生成の後に参照する object の実際の列で確かめる（下の「列の検査」） |
| 接続 | system_admin と同じく、アプリ用の DB ユーザーのネイティブの接続だけ（SELECT も `OracleNl2SqlAdapter.execute_select(admin_control_plane=True)` で `connection()` を使い、業務の実行の pool を使わない） |
| 実行 | 生成した SQL を必ず表示し、自動では実行しない。サーバーが文に分け（PL/SQL のブロックは 1 文）、文ごとに分類・実行する（#2333。下の「複数の文」）。読み取り専用の SELECT / WITH…SELECT の文は「実行」で実行する（確認語は要らない）。データ・構造を変える文は実行確認語 `ADMIN_EXECUTE`（1 つの対象を消す `DROP` / `TRUNCATE` の文は対象の名前。例 `APP.T1`）を入力したときだけ実行する。確認は 1 回ごと（実行したら消える）で、SQL が変わる（AI のエラー分析で直した SQL を使う）と確認からやり直す |
| 分類 | 生成エンジンに関係なく、実行の前にサーバーで必ず分類する（`_split_sql_statements`・`_admin_statement_type`・`is_select_only`）。生成の応答（`plan`）も実行の前に作り直す（画面の値を信じない） |
| 実行の実装 | 1 文の実行は「管理 SQL を実行」の `_execute_db_admin_sql` にその文だけを `chat=True` で渡す（PL/SQL・暗黙の commit の警告・DB 構造の再取得は同じ。DML も部分の commit の経路（`data_dml`）へ渡さない）。「すべて実行」は `OracleNl2SqlAdapter.execute_admin_script` が 1 つの管理の接続とトランザクションで上から順に実行し（SELECT は同じ cursor から読むので、前の文の commit 前の変更が見える）、最初のエラーで止め、**全部成功したときだけ commit**、失敗したら rollback する（DDL は Oracle の暗黙の commit で戻らない。その前の成功した DML も commit される） |
| 止める文 | 製品の object（`NL2SQL_` / `PLATFORM_` / `RAG_` / `AGENT_` で始まる名前、`$` / `#` を含む名前）と動的 SQL は「管理 SQL を実行」と同じく止める。例外は読み取り専用の SELECT の動的性能ビュー（`V$` / `GV$`。所有者は無指定・SYS・PUBLIC）だけで、セッション・ロック・版の質問に要るので読ませる（`SYS.OBJ$` などほかの `$` の object は止める）。加えて、方針で止める文を技術の規則としてコードに書く（下の表） |
| 失敗 | 失敗した文の区画の「AI でエラーを分析」で原因と直した SQL を示す（`POST /api/nl2sql/db-admin-chat/conversations/{id}/turns/{turn_id}/analyze-error` に文の番号を渡し、サーバーが保存した SQL・失敗・同じ文の過去の失敗と、参照する object の実際の列を分析する（#2352・#2950）。OCI Enterprise AI が無いときは ORA のコードからの分析）。直した SQL はその文だけを置き換え、サーバーが分類し直し、確認語からやり直す |
| 会話 | 「チャット」と同じく利用者ごとに保存し、左の「会話の履歴」から開き直せる（#2319。下の「会話の履歴」）。続きの質問の文脈（前の 6 往復の質問・SQL・最後の実行の結果の要約）は、サーバーが保存した会話から作る（画面から受け取らない） |
| 監査 | 表 `NL2SQL_ADMIN_AUDIT_LOG`（migration 023）に、実行した人・SQL の全文・文の種類・確認語の有無・結果（success / partial / failure / blocked）・commit / rollback・影響行数・時間・経路（`db_admin_chat` / `admin_sql` / `data_preparation`）を件数の上限なく残す。DB 管理チャットは**実行した文ごとに 1 行**で、`detail` の `batch_id`・`batch_mode`（`statement` / `all`）・`statement_index`・`statement_total`・`conversation_id`・`turn_id`（「すべて実行」は `batch_committed`・`batch_rolled_back` も）で同じ実行の行を結び付ける。SELECT の実行と止めた文も残す。確認語の待ちだけの応答と、「すべて実行」で実行しなかった文は残さない。「AI 活用 › 実行履歴」には出さない（実行履歴はチャット・SQL 生成・MCP の実行だけ） |
| 公開 | 画面だけの API（Cookie / CSRF）。外部の MCP・Agent・API キーからは使えない。四眼原則（別の人の承認）は持たない |

## 複数の文（#2333）

利用者の判断で、SQL Developer・DataGrip・DBeaver のワークシートと同じく、生成した SQL を文ごとに扱う（#2264 は複数の文に SELECT を含むとターン全体を止めていた）。

- **分け方**: サーバーが「管理 SQL を実行」と同じ `_split_sql_statements` で分ける（PL/SQL の無名ブロック・`CREATE PROCEDURE` などは 1 文）。分類（`_admin_statement_type`・`is_select_only`）と止める文の判定も文ごと。
- **既定は 1 文ずつ**: 文ごとの「実行」は、その文だけを実行する。
- **確認は件数ではなく危険度**: SELECT / WITH…SELECT は 1 文でも、SELECT だけの「すべて実行」でも確認語なし。データ・構造を変える文は確認語（上の「実行」の規則）。変える文を含む「すべて実行」は確認語を 1 回だけ入れる（変える文が 1 つならその文の確認語、2 つ以上なら `ADMIN_EXECUTE`）。
- **止めるのは許されない文だけ**: 止める文はその文だけを「実行できない」にし、同じターンのほかの文は 1 文ずつ実行できる。**「すべて実行」は止める文が 1 つでもあれば始めない**（画面は押せず理由を出し、API は 400）。黙って飛ばすと、利用者が並べた順の前提（作ってから入れる・確かめてから消す）が崩れるため。
- **「すべて実行」の停止と結果**: 上から順に実行し、最初のエラーで止める。失敗した文と実行しなかった文（`not_run`）を文ごとに示す。
- **生成の指示**: 読むことが複数あるときは 1 つの SELECT に 1 つのことを読ませ、文を並べる（UNION で 1 つにまとめない）。結果を文ごとに並べて読めるようにするため。

## 列の検査（#2352）

モデルはデータ辞書・動的性能ビューの列を記憶から書き、無い列で `ORA-00904` になる（例: `DBA_ROLE_PRIVS` の `USERNAME`、`V$LOCK` の `LOCK_TYPE`・`MODE_HELD`）。生成の文脈には業務の表の列しか無いので、**生成の後に、参照する object の実際の列で確かめる**。列の一覧はコードにもプロンプトにも書かない。実装は `backend/app/features/nl2sql/db_admin_chat_columns.py`（純粋な処理）と `OracleNl2SqlAdapter.admin_sql_describer`（調べる実装）。

- **対象**: 方針で止めない SELECT と DML（INSERT・UPDATE・DELETE・MERGE）の文。方針で止める文（製品の object など）は調べない。同じターンの前の文（SELECT・DML 以外。DDL・GRANT など）に名前が出る object を参照する文は、実行の前は object が無い・列が違うので確かめない。Oracle の実行環境（`NL2SQL_RUNTIME_MODE=oracle`）だけで確かめる。
- **取り出し**: sqlglot（Oracle の方言）で参照する object と列を取り出す（別名・`*`・関数・副問い合わせ / CTE・引用識別子・`SERIAL#` のような名前。相関副問い合わせは外側の参照先、ORDER BY の別名・疑似列（`ROWNUM`・`SYSDATE` など）は列にしない）。表関数・`JSON_TABLE`・DB link・`*` の副問い合わせ・PIVOT など、列を確かめられない参照は確かめない。
- **調べ方**: 実行と同じ管理の接続（アプリ用の DB ユーザー）で、`SELECT * FROM <object> WHERE 1 = 0` を `cursor.parse`（実行しない解析）して `cursor.description` の列を読む。データは読まない。調べた列は process ごとに cache する（10 分。調べられない object は 1 分。DDL・権限の変更を実行したら捨てる）。
- **止めるかどうか**: 文そのものを `cursor.parse`（実行しない）し、Oracle が `ORA-00904` を返したときだけ止める。sqlglot の静的な検査は、存在しない列の一覧とどの object の列かの説明に使う（静的な検査が誤る文を止めないため）。
- **直させる**: 存在しない列（`ORA-00904`）か、Oracle の解析で見つからない object（`ORA-00942` など）を参照する文は、文の番号・SQL・エラー・参照する object の実際の列を渡して、**1 回だけ**モデルに直させる（指示は `prompts/db_admin_chat/repair.txt`、構造化出力は `DbAdminChatRepairOutput`）。直した文だけを置き換えて分類し直す。
- **直らなかったら**: 文は残し、`blocked_reason` に「列が存在しません: X（<object> の列: …）」、`unknown_columns` に列を入れ、その文の実行と「すべて実行」を止める（API の実行も止めて、止めた文として監査に残す）。画面はその文の区画に理由と「AI でエラーを分析」を出し、直した SQL に置き換えると分類し直す（`PUT …/plan` も同じ検査をする）。
- **調べられない object**（存在しない・読む権限が無い）は止めず、文の `warnings` とターンの警告に「<object> の列を確かめられませんでした（存在しないか、読む権限がありません）。」を出す。

## 方針で止める文

`backend/app/features/nl2sql/db_admin_chat.py` の `FORBIDDEN_STATEMENT_RULES`。文字列リテラル・引用識別子・コメントの中身を空白にした文に当てる（値の中の語では止めない）。**DB 管理チャットだけに当て、「管理 SQL を実行」には当てない**（手で書く管理の経路として、今までどおり `ALTER SYSTEM KILL SESSION` などを ADMIN_EXECUTE で実行できる。LLM が作る SQL にだけ追加の柵を置く）。

| 規則 | 止める文 |
|---|---|
| `ALTER_SYSTEM` | `ALTER SYSTEM` |
| `ALTER_DATABASE` | `ALTER DATABASE` / `ALTER PLUGGABLE DATABASE`（`DATABASE LINK` は除く） |
| `CREATE_DROP_DATABASE` | `CREATE` / `DROP` の `DATABASE` / `PLUGGABLE DATABASE` |
| `SHUTDOWN_STARTUP` | `SHUTDOWN` / `STARTUP` |
| `USER_ACCOUNT` | `CREATE` / `ALTER` / `DROP USER` |
| `DROP_TABLESPACE` | `DROP TABLESPACE` |
| `AUDIT_POLICY` | `NOAUDIT`、`CREATE` / `ALTER` / `DROP AUDIT POLICY` |
| `SESSION_STATE` | `ALTER SESSION`（接続の状態を変える） |
| `TRANSACTION_CONTROL` | `COMMIT` / `ROLLBACK` / `SAVEPOINT` / `SET TRANSACTION`（commit は実行の仕組みが決める） |
| `ADMIN_PRIVILEGE_GRANT` | `DBA`・`PDB_DBA`・`SYSDBA` などの管理者のロール・権限、`ALL PRIVILEGES`・`ANY PRIVILEGE` の付与と取り消し |
| `DYNAMIC_SQL` | `EXECUTE IMMEDIATE`・`DBMS_SQL`・`OPEN … FOR`（文のどこでも） |
| `EXTERNAL_ACCESS_PACKAGE` | `UTL_HTTP`・`UTL_TCP`・`UTL_SMTP`・`UTL_MAIL`・`UTL_FILE`・`UTL_INADDR`・`DBMS_NETWORK_ACL_ADMIN`・`DBMS_CLOUD`・`DBMS_CREDENTIAL`・`DBMS_JAVA`・`DBMS_SCHEDULER`・`DBMS_JOB`・`DBMS_PIPE`・`DBMS_LDAP`・`HTTPURITYPE`（SELECT の中でも止める。外部への送信・ファイル・ジョブ・資格情報） |

## API

| API | 権限 | 内容 |
|---|---|---|
| `POST /api/nl2sql/db-admin-chat/generate` | `menu.db_admin_chat` | `{question, conversation_id, client_turn_id}` → `{conversation, turn}`。SQL を作ってターンとして保存し、実行はしない。`conversation_id` が空なら新しい会話（会話の ID は最初のターンの ID）。同じ `client_turn_id` の送り直しは保存済みのターンを返す（二重に生成しない）。画面が停止した（接続が切れた）生成は保存しない。モデルが無いと 409、生成の失敗は 502、往復の上限（50）は 409、他人・無い会話は 404 |
| `POST /api/nl2sql/db-admin-chat/execute` | `menu.db_admin_chat` | `{conversation_id, turn_id, mode, statement_index, confirmation, row_limit}` → `DbAdminChatExecuteData`（`mode`・`batch_id`・`committed` / `rolled_back`・`stopped_at`・文ごとの `statements[]`（`index`・`status`: succeeded / failed / blocked / not_run / confirmation_required・その文だけの `result`（管理 SQL を実行と同じ `DbAdminExecuteData`））・DB 構造の再取得・警告）。`mode="statement"` は `statement_index`（1 始まり）の 1 文、`mode="all"` はすべての文（止める文を含むと 400）。SQL は画面から受け取らず、ターンに保存した SQL を実行し、ターンに文ごとの最後の実行の要約を残す。確認語が合わなければ `confirmation_required` を返し実行しない（要約にも残さない） |
| `PUT /api/nl2sql/db-admin-chat/conversations/{id}/turns/{turn_id}/plan` | `menu.db_admin_chat` | `{sql, statement_index}` → ターン。AI のエラー分析で直した SQL に置き換え（`statement_index` があればその文だけ。無い番号は 400）、サーバーが分類し直す（列の検査もし直す。版を進め、変わった文の実行の要約と「すべて実行」の要約は消す。番号と SQL が同じ文の要約は残す） |
| `GET /api/nl2sql/db-admin-chat/conversations` | `menu.db_admin_chat` | 会話の履歴（新しい順・offset 型。既定 25 件）。system_admin は全員の会話（利用者の表示名つき） |
| `GET` / `PATCH` / `DELETE /api/nl2sql/db-admin-chat/conversations/{id}` | `menu.db_admin_chat` | 会話の内容（ターン）・名前の変更（1〜80 文字）・削除（ターンも消える。監査の表は残す） |
| `POST /api/nl2sql/db-admin-chat/conversations/{id}/turns/{turn_id}/analyze-error` | `menu.db_admin_chat`・会話の所有範囲 | `{statement_index}` → `{analysis, corrected_plan, previous_attempts, warnings}`（保存した失敗を使う） |

## 会話の履歴（#2319）

利用者の判断で、#2264 の「会話は保存しない」を改め、「チャット」と同じく会話を履歴に残す。

- **保存先**: 製品の専用の表 `NL2SQL_DB_ADMIN_CHATS`（会話。持ち主・名前・時刻）と `NL2SQL_DB_ADMIN_CHAT_TURNS`（ターン。会話の FK の ON DELETE CASCADE。内容は JSON）。migration 024（`backend/migrations/024_db_admin_chat_conversations.sql`）、保存の実装は `backend/app/features/nl2sql/db_admin_chat_store.py`（Oracle を使わない構成は同じ契約の memory）。
- **チャットのジョブを流用しない理由**: チャットの会話は SQL 生成のジョブ（`NL2SQL_STATE_DOCUMENTS` の `jobs`）で、実行履歴・フィードバック・品質評価・業務プロファイルの範囲・worker と結び付く。DB 管理チャットは業務プロファイルを持たず、生成は同期の API で、実行の記録は監査の表（migration 023）にあるので、同じ印で混ぜると実行履歴などに DB 管理の往復が出る。専用の表なら持ち主・時刻の索引と、会話を消すとターンも消える FK を持てる。
- **ターンに残すもの**: 質問・送った時刻・生成した時刻・説明・警告・生成に渡した表・モデル・生成した SQL とサーバーの文ごとの分類（`plan`）・版（`revision`）と AI で直したか・文ごとの最後の実行の要約（`statement_executions`: 文の番号・時刻・1 文か「すべて実行」か・`batch_id`・確認語を入れたか・状態（succeeded / failed / blocked / not_run）・行数・列数・打ち切り・続きの質問の文脈の要約・その文の結果と commit / rollback）・最後の「すべて実行」の要約（`last_run_all`: 成功の数・止めた文・commit / rollback・警告）・持ち主以外が送ったときの送り手。**結果の行と確認語は残さない**（チャットと同じく、開き直したら文ごとに「前回の実行（…）: N 行・M 列。…」と「もう一度実行」。確認語は 1 回ごと）。
- **見え方**: 「AI 活用」と同じ（system_admin は全員の会話、それ以外は自分の会話だけ。他人の会話は一覧に出さず、詳細・操作は 404）。system_admin が他の利用者の会話にできるのは閲覧と削除だけで、続きの質問・実行・SQL の置き換え・エラーの分析・名前の変更は持ち主だけ（403。#3244）。#3244 より前に持ち主以外が送った質問には送り手を添える。メニュー権限は `menu.db_admin_chat` のまま。
- **保持**: チャットと同じく期限で消さない（利用者が削除するまで残る。保持期間の設定は無い）。
- **監査**: 監査の表（migration 023）の意味は変えない。会話を消しても監査の行は残る。

生成と分析は LLM を同期で呼ぶので、Nginx の長い待ち時間の API（`platform/deploy/suite-nginx.sh` の `SUITE_NL2SQL_LONG_API_PATHS`）に入れる。画面の待ち時間は `API_TIMEOUT_MS.longRunningJob`。

## 画面

「チャット」と同じ部品・同じ文言で描く（2 つの画面で書き写さない。`frontend/src/features/nl2sql/components/ChatScaffold.tsx`）。

- 骨格は共通の `ChatLayout`、入力欄は共通の `ChatComposer`（Enter で送信・Shift+Enter で改行・生成中は停止。停止した生成は保存しない）、送った質問は `ChatPendingTurn`（生成の失敗は質問を残して同じターンの ID で「再送信」）。
- 会話の履歴はチャットと同じ（#2319）: 会話の欄の上端の行の左端にアイコンだけの開閉ボタン、デスクトップ（lg 以上）は左のパネル・狭い画面は `SideSheet`、一覧は共通の `ChatHistoryList`（チャットと共通の `Nl2SqlChatHistoryList`。日時・system_admin には持ち主の表示名・offset 型のページング 25 件）、名前の変更・削除は共通の `useChatHistoryActions`、右端に「新しい会話」。選んだ会話の名前を上端に出し、system_admin が他の利用者の会話を開いたときは、入力欄・実行・分析・名前の変更を出さず、入力欄の代わりに閲覧だけの案内を出す（削除は出す。#3244）。画面はチャットと同じく常時マウントし、会話 ID・履歴の開閉とページ・会話ごとの下書きを作業状態に残す（[frontend-workspace-state-spec.md](./frontend-workspace-state-spec.md)）。
- 回答は `ChatTurn` / `ChatAnswer`。処理の経過は共通の `ChatProgress` で、段階の名前はチャットと同じ定義（生成 → SQL の安全性の確認（サーバーの分類））。**「実行」「もう一度実行」「すべて実行」は処理の経過に足さない**（#2333。経過は生成したときのまま。段の数・合計時間を変えない）。実行中の経過時間と結果の要約（N 行・M 列・時間、失敗）はその文の区画（「すべて実行」は「すべて実行」の区画）に出し、もう一度実行するとその区画だけを差し替える。開き直した会話は、元の経過と前回の実行の要約を別に出す。説明と AI の分析の文は共通の `MarkdownText` で描く（Markdown の記号を生で出さない）。
- SQL は文ごとの区画に `ChatSqlBlock`（番号「文 N」（2 文以上のとき）・分類のバッジ（読み取り専用 / 変更あり / 実行できない。アイコンつき）・文の種類・コピー・実行・本文）。SELECT の「実行」は SQL の横（チャットと同じ位置）、確認語の要る文は警告（変更すること・DDL は暗黙に commit されて元に戻せないこと）と共通の `ExecutionConfirmationField` の横。止める文はその区画に理由の `Banner` を出し、実行の操作を出さない（ほかの文の区画は実行できる）。
- 結果はその文の区画の直下に出す。SELECT の結果はチャットと同じ `ChatQueryResult`（共通の `ChatResultTable`。要約・プレビュー・すべての行・CSV）。更新系の結果・失敗は「管理 SQL を実行」と同じ `DbAdminExecutionResult`（状態・影響行数・commit / rollback。SQL はすぐ上にあるので繰り返さない）。「すべて実行」で実行しなかった文は「実行しなかった」。DDL の後の DB 構造の同期の案内はターンの末尾。開き直した会話の SELECT は、結果の行を保存しないので前回の実行の要約（チャットと共通の `ChatLastExecutionText`）と「もう一度実行」を出す。
- 2 文以上のターンは、文の区画の上に「すべて実行」の区画を出す（文の数と「1 文ずつ実行するか、すべて実行で上から順に実行（最初のエラーで止める）」の案内）。SELECT だけならボタンだけ（確認語なし）。変える文を含むなら、トランザクションの扱いの警告と、確認語の欄（1 回だけ入れる）の横に「すべて実行」。止める文を含むなら「すべて実行」は押せず、ボタンの直下に止める文の番号と理由を出す。結果の要約（すべて成功 / 文 N で停止・commit / rollback・成功と実行しなかった文の数・警告）はこの区画に、文ごとの結果は各文の区画に出す。

## アプリ用の DB ユーザーの権限

データ辞書・動的性能ビューは `PDB_DBA` の範囲で読む（[../../terraform/README.md](../../terraform/README.md)「アプリ用の DB ユーザー」）。開発の ADB（ADMIN の接続）では `V$VERSION`・`PRODUCT_COMPONENT_VERSION`・`DBA_TABLESPACE_USAGE_METRICS`・`DBA_DATA_FILES`・`DBA_FREE_SPACE`・`V$SESSION`・`DBA_OBJECTS`・`DBA_ROLE_PRIVS`・`DBA_USERS`・`V$LOCKED_OBJECT`・`DBA_SEGMENTS`・`V$PARAMETER` が読めた。アプリ用の DB ユーザー（`ADB_APP_USER`）での確認は検証環境で行う（読めないビューは実行の失敗として分析が代わりのビューを示す）。監査の表（migration 023）と会話の履歴の表（migration 024）は製品の migration が作るので、追加の grant は要らない。


## 修正を繰り返す場合（#2950）

文の実行が失敗したら、ターンの JSON に文の番号・版・SQL・エラーコード・先頭行の要約・実行時刻を残す。結果の行や確認語は残さない。同じ版の再実行は最後の失敗で置き換え、文ごとに今の失敗と、その前の直近 3 件を保つ。SQL の差し替え時も、保存済みの最後の失敗を履歴へ移す。

分析は会話の所有範囲を確かめ、対象の文の過去の失敗を使う。SQL のコメント・空白・引用しない語の大小文字・末尾のセミコロンだけを正規化し、すでに失敗した SQL と同じ修正は 1 回だけ作り直す。文字列リテラルと引用した識別子の内容は変えない。再度同じ SQL なら修正案を返さず警告する。画面の「これまでの修正」で、前の版と失敗を開いて確認できる。

失敗の分析は `cause`（原因）・`fix`（直し方）・`conclusion`（結論）の 3 項目と `corrected_plan` を返します。画面は項目ごとに見出しを付け、本文を Markdown で表示します。エラーコードからの分析も同じ項目で返します。
