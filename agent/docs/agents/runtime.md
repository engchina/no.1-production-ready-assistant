# Agent のアーキテクチャ不変条件

> agent/AGENTS.md から移した詳細（#2276）。agent/AGENTS.md の規則と同じ強さで守る。規則を変えるときはこのファイルを直し、AGENTS.md には規則の要約と案内だけを書く。

## アーキテクチャ不変条件

- 実行は Control Plane の**組み込み Runtime**（`features/agent/builtin_runtime.py`）。
  - SDK は OpenAI Agents SDK（`openai-agents`。版を固定し、Dependabot の PR で上げる）。
  - モデルは OpenAI 互換のモデルの API の Responses API（システム設定 > モデルの接続）。別の LLM の SDK は組み込まない。
  - SDK の tracing は常に無効にする（業務データを外部へ送らない）。
- 外部の Runtime（OpenClaw / Hermes / DeerFlow など）・Binding・Docker の Runtime は持たない（#754）。
- 指示は「Agent の指示 + 割り当てた Skill の指示」。ツールは Skill の requirement が必要とするものだけを function tool として渡す。`server_id="control-plane"` は `tool_registry` のツール、それ以外は MCP 接続（`rag` / `nl2sql` / 登録した接続）の `tools/list` のツール（名前は `<接続>__<ツール>`）。Skill の指示は MCP のツールを素の名前（`rag_search`）で書いてよく、Run のときにモデルへ渡す名前へ書き直す。モデルが無い名前のツールを呼んでも Run は落とさず、呼ぶ名前をモデルへ返して呼び直させる（読み替えて実行はしない。#1303）。
- ツールの実行は `tool_registry.invoke`（ポリシー・ガードレール・監査・成果物）を必ず通す。
  - ポリシーが「拒否」のツールはモデルに渡さない。
  - 「承認」のツールは SDK の `needs_approval` で中断する。中断した Run は `waiting_approval` にし、SDK の状態を Run に保存する。
  - 回答前の確認（`ask_user_confirmation`）は、利用者に聞けるならどの経路でも中断して `waiting_confirmation` にする（#3352）。中断はツールを実行する前に応答ごとに 1 回決め（`on_llm_end`）、同じ応答のほかのツールは確認の答えまで実行しない（`tool.confirmation_pending`）。確認の呼び出しを中断せずに「確認していない」と返すのは、ツールの確認のカード・失敗の直後・表と列の確認・スキップ・上限だけ。
  - 承認がすべて決まったら状態を復元して再開する。却下したツールは実行しない。
  - 呼び先へ送る直前の境界は、通常の実行・承認の再開・MCP 接続のツール・登録済みのツールで同じ順に通す（`_ToolRecorder.invoke`。#2609・#2610・#2630）: ① 取消（終わった Run と、別の実行に置き換わった実行〔実行の token が違う。#2606〕では step を作らず handler を呼ばない。別のプロセスの取消も保存先の Run の行で判定し、書き込みでも取消を勝たせる。モデルの呼び出しの前も同じ判定で止める）→ ② 引数（壊れた JSON・object 以外を空の引数にせず、モデルに見せた schema で型・必須・enum・範囲・追加の項目・入れ子を確かめる。検索の件数の正規化の後に確かめ、実行環境が埋める非表示の引数は求めない。呼び先の契約は `tool_registry.invoke` も確かめる。外部の `$ref` は取りに行かない。誤りは値を含まない `argument_errors`（場所・種類・直し方）で返す。`tool_arguments.py`）→ ③ ツール権限・承認（SDK の `needs_approval` は呼び出しごとに最新のツール権限で決める。「承認」のツールは、その Run・呼び出し ID・ツール・引数の承認の記録がある呼び出しだけを実行する）→ ④ handler。
  - 取消の前に送った操作は取り消せない。結果が取消の後に届いたら事実のまま記録し（イベントに `run_status`）、取消のイベントに送っていた step（`in_flight_step_ids`）を残す。
  - 実行中の呼び出しは取消で打ち切る（#2691。`call_abort.py`）: ツールの呼び出しごとの打ち切りの印を文脈に置いて thread で呼び、同じプロセスの取消（`_cancel_run_locked`）はすぐに、別のプロセスの取消は呼び出しの間の定期の確認（`TOOL_CALL_CANCEL_POLL_SECONDS`。保存先の Run の行）で、この task の取消（SDK のツールの timeout）でも印を立てる。MCP の送信は印が立つと送らず・再試行せず、送信中の接続は状態を変えない手順と読み取り専用（注釈を信頼する接続の `readOnlyHint`）のツールだけ socket を `shutdown` して切る（書き込みのツールは送った後は結果を待つ）。打ち切った呼び出しの step は失敗ではなく取消（`runtime.tool_call_aborted`。取消から戻るまでの秒を `error_details.stopped_after_seconds` とログ `builtin_runtime_tool_call_aborted` に残す）。後始末（`cleanup`）の呼び出しは打ち切らない。
  - 取り消した Run が始めた NL2SQL のジョブは止める（#3318。`job_cancel.py`・`builtin_runtime.cancel_run_nl2sql_jobs`）: Run の step の `nl2sql_query` / `nl2sql_get_job` の結果で、ジョブごとの最後の状態が pending / running のもの（取消を送ったジョブは除く）に、Control Plane が `nl2sql_cancel_job` を後始末（`cleanup`）の step として Run の利用者のサービストークン（Run の版のデータの範囲の claim。ジョブを作ったときと同じ）で送る。予算・ツールで確かめた回数には数えず（trace_id は `run_cancel_`）、ポリシーの「拒否」は守る。実行中だった Run は実行している側が Run の終わりに送り（取消の後に届いた `nl2sql_query` の結果のジョブも分かる）、実行している側の無い Run（承認・回答前の確認の待ち・再開の待ち）は取消の入口（画面の `POST /runs/{id}/cancel`・MCP の `agent_cancel_run`）が予約する。止められなくても取消は済んでいる（ログ `builtin_runtime_nl2sql_job_cancel_failed`）。RAG などの読み取りのツールの扱い（接続を切る）は変えない。
  - ツールの結果の機微情報（機微な名前の項目・文の中の資格情報・Bearer・個人情報）は、成功の出力と同じ規則で、失敗の文・詳細（`error` / `error_details`。呼び先の応答の本文・JSON-RPC の error・例外の文・入れ子の詳細、文の中の URL の資格情報も）からも `tool_registry.invoke` の結果の境界（`_tool_result` → `sanitize_tool_error`）で伏せる（#2611）。モデルへの結果・Run の step・イベント・Run の失敗の文・ログはこの結果を使う。`argument_errors`（値を含まない）と有無を表す真偽の項目は残し、伏せた種類は `audit_metadata.error_masked` に名前だけ残す。
  - ツールの結果の画像（#2650。`tool_media.py`。MCP に依らない型で、MCP 接続・会話の添付などどのツールの結果でも同じ経路）: handler は `ToolOutputWithImages`（文字の結果と `ToolImage`）を返し、`tool_registry.invoke` が画像を `ToolResult` の非保存の属性（`tool_result_images`）に分ける。step・イベント・ログ・成果物には参照（`content_images` の `image_id`＝sha256・MIME・大きさ・由来〔接続・ツール・content の位置、RAG なら根拠の ID・文書の版 `chunk_set_id`・`region`（page・bbox）〕）だけを残し、base64 を残さない。`_ToolRecorder.deliver_images` が登録モデルの「画像対応」（`ModelTarget.vision_enabled`）と 1 回の結果の上限（`MAX_IMAGES_PER_RESULT`）で添付する画像を決め、参照に `model_input`（`attached` / `unsupported_model` / `over_limit` など）と、読めなかったときの `image_input_note` を書き、見積もりを `audit_metadata.image_input` に残す。添付する画像は SDK の function tool の出力（`[ToolOutputText, ToolOutputImage]`。Responses API の `function_call_output` の `input_image`）で同じ呼び出しの次のモデルの入力に渡す。承認待ちで保存する SDK の状態からは画像のデータを外し（`strip_image_data`。読み直しの案内の文にする）、再開・会話の続きには画像を持ち越さない（要るときはツールで取り直し、呼び先が毎回認可する）。画像に対応しないモデルの代わりに既定の Vision モデルで読む編成は持たない（#2651・#2652）。
  - 進展の無いツールの呼び出しは Run を終える（#2580。`tool_loop_guard.py`）。保存した step から、モデルが呼んだツールごとに、同じ引数で同じ結果が `REPEAT_LIMIT` 回（`runtime.tool_call_repeated`）・実行の失敗が成功を挟まずに `FAILURE_LIMIT` 回（`runtime.tool_failures_repeated`）・引数の誤り（schema・呼び先の検証・データの範囲）が `ARGUMENT_ERROR_LIMIT` 回（`runtime.tool_argument_errors_repeated`）続いたかを数える（結果が変わる polling・引数が変わるページング・別のツールの失敗・承認待ち・取消・予算の超過・Control Plane の呼び出しは数えない。並列の呼び出しは始めた順）。上限の 1 回前の結果に `loop_notice` を足して差し戻し、上限に達したら同じ応答の残りの呼び出しを実行せず、次のモデルの呼び出しの前に Run を失敗にする（イベント `runtime.failed` の `error_code`・ログ `builtin_runtime_tool_loop_stopped`・metric `agent_tool_loop_stops_total`。引数・結果の値は出さない）。
- RAG の根拠のツール（`rag_search` / `rag_retrieve_evidence`）の縮退の使い方（#3546。`features/agent/rag_retrieval_state.py`）: 根拠が 0 件で任意の補助の処理の縮退（`degraded_steps`）があるときは、モデルへのツールの結果に案内（`retrieval_notice`。資料に記載が無いとは答えない）を足す（記録する step の結果は RAG のまま）。回答の対応の `absence_conclusion` はその `rag_search` を数えず、縮退した検索を使ったこと（`rag_degraded`）・表の行・列の対応の検査が読めない根拠（`table_binding=not_checkable`）で裏付けたこと（`table_binding_unchecked`）は対応の値を変えない記録だけのシグナルにする。
- モデルに渡す入力は、モデルの入力の上限に収める（#2383。`features/agent/context_window.py`。SDK の `call_model_input_filter`）。
  - 予算は登録モデルの `max_input_tokens`（システム設定 > モデル）から、出力の上限（入力の上限の半分まで）と余白（`AGENT_CONTEXT_SAFETY_MARGIN_RATIO`）を引いた token の数。見積もりは platform の `estimate_input_tokens` × `AGENT_CONTEXT_TOKEN_ESTIMATE_SCALE`。
  - 超えるときは古いツールの結果から段階的に縮め（一覧の項目の長い文字列を先頭だけ → 後ろの項目を ID だけ → すべて ID だけ）、最新のツールの結果は古い結果を縮めても足りないときだけ縮める。利用者の質問・会話の履歴・指示は縮めない。ID の項目（`id`・`*_id`）は消さず、縮めた結果に `context_compaction`（元の文字数と次の手）を足す。次の手は、RAG の根拠なら `rag_read_source` で読み直す、信頼できる読み取り専用のツールなら絞って呼び直す、副作用があり得るツール（書き込み・注釈を信頼しない接続）なら呼び直さずに残った内容で続ける（同じ操作を再実行させない。#2632）。
  - 画像（#2650）は大きさからの見積もり（`tool_media.estimate_image_tokens`。登録モデルの画像の扱いに依らない多めの値。画像を添付した呼び出しは見積もりとプロバイダの数えた入力をログ `builtin_runtime_model_input_measured` に並べる）で数える。同じ画像は最新の結果の 1 つだけに添付し、入力の画像は新しい結果から `MAX_IMAGES_IN_INPUT` 枚まで。予算を超えるときは古い結果の画像を文字を縮める前に外し、最新の結果の画像は文字を縮めても足りないときだけ外す。外した画像は ID・理由・読み直しの手の文に置き換え、`context_compaction` の記録に `images_removed` / `image_reason` を残す。
  - 縮めるのはモデルに渡す入力だけで、Run の step のツールの結果は全文のまま（回答の検証・引用の照合は step を使う）。縮めたことは Run のイベント（`context_compaction`）・metric `agent_context_compactions_total`・ログに残す。
  - 縮めても収まらなければ、モデルを呼ばずに `runtime.context_overflow`（対象を絞る・入力の上限の大きいモデルを選ぶ案内）で失敗させる。プロバイダが入力の超過の 400 を返したら、返った prompt の token 数で Run の残りの見積もりを補正して縮め、1 回だけ送り直す（2 回目も超過なら `runtime.context_overflow`）。
  - モデルの呼び出しごとの入力は、モデルの呼び出しの span（`gen_ai.chat`。送り直しは `pr.attempt` の別の span）の属性に残す（#2583。`model_call_observation.py`。本文は含めず、属性は platform の allowlist の名前だけ）: 指示の変わらない部分（業務 Agent の指示・スキル・データの範囲の案内。`build_sdk_agent` が Run の状態を足す前の指示）・指示の全体・ツールの定義の集合の指紋（`pr.context.instructions.static_fingerprint` / `.fingerprint`・`pr.context.tools.fingerprint`。sha256 の先頭 16 桁）、入力の部分ごとの見積もり（`pr.context.estimate.*`: 変わらない指示・Run の状態・ツールの定義・会話の履歴・ツールの結果と合計）、Run の予算と残り（`pr.context.budget.*`）、縮める前後と縮めた数（`pr.context.compaction.*`）。指紋の一致は cache の hit の証明ではない。cache の実測は usage の `gen_ai.usage.cache_read.input_tokens` で、プロバイダが返さなければ付けない（不明。0 と区別する）。呼び出しごとの usage は span だけに、Run の累計は SDK の `Usage`（`RunUsage`。承認の再開・差し戻しも累計を引き継いで上書き）だけに持ち、互いに足さない。送り直した試行のうち未完了・入力の超過の span は失敗で、累計に入るのは完了した span の usage だけ。
- 業務 Agent は版を持つ。編集は下書きで、「公開」した版だけが利用者の Run に使われる。「この版に戻す」で前の版を公開し直す。下書きで実行できるのは Agent 管理（admin）と、個人の業務 Agent の持ち主だけ（#2523）。会話は始めた Run の種類（下書き / 公開中の版）に固定し、下書きの会話はチャットの会話の履歴に出さない（編集画面の「試す」の履歴に出す。#2515）。
  - 個人の業務 Agent（#2523）: `agent.agents.build` だけを持つ利用者が作った業務 Agent は、作った人が持ち主（`owner_user_uuid`）になる。持ち主の Agent は対象範囲に足し（`owned_agent_ids`）、持ち主は編集・下書きで実行・評価・公開・削除ができる。共有は持ち主の申請（`POST /agents/{id}/share-request`）を Agent 管理が承認すると組織の業務 Agent（持ち主なし。元の持ち主は変更できない）になり、権限管理でロールに割り当てる。承認前の個人の Agent はロールに割り当てられない（409、権限管理の候補に出さない）。
  - 出力の項目（#2549。`output_fields`、版の項目）: 業務 Agent に key・表示名・型（文字 / 数値 / 日付 / 真偽）・必須・説明を最大 30 項目まで定義すると、その Run にだけ `record_output` を渡す（引数の schema は項目から作る。`output_fields.py`）。値の検証はコードで行い、項目ごとの誤りを `argument_errors` でモデルに返す。正しい記録は成果物 `structured_output`（`{columns, rows}`。呼び直すと置き換える）に残し、チャットに表と CSV で出す。記録せずに回答した Run は 1 回だけ差し戻す（`_require_output`。経過には `output_retry`）。それでも記録しなければ、画面が記録の無いことを示す。品質評価は、評価ケースの期待する出力（`expected_output`）を、記録した出力とコードで項目ごとに比べ（文字は全角半角・大小・空白を無視、数値は桁区切りも数、日付は ISO、真偽は はい / いいえ も。1 列目の値で行を組む）、項目の比べ方（`match`）が「意味が同じ」の文字の項目で文字が一致しなかったものだけを、ケースごとに 1 回の LLM の呼び出しでまとめて判定し（#2562。判定の理由は `reason`（#2568）、失敗したら文字の一致の結果のまま `note` に理由）、ケースの `field_results` / `field_correct` と概要の `field_accuracy` / `field_item_accuracy` に残す。合格率（公開前の評価の門）には含めない（#2558）。
  - 公開前の評価の門（#2519）: `AGENT_PUBLISH_MIN_PASS_RATE`（0〜1。既定 0 = 使わない）が 0 より大きいと、`POST /agents/{id}/publish` は、下書きを最後に変えた時刻（`draft_updated_at`。版の項目が変わったときだけ進む）の後に始めた下書きの評価（完了したもの）の最新の合格率を求め、足りなければ 409 `publish_evaluation_required`。回帰の門（#2569）: `AGENT_PUBLISH_MAX_PASS_RATE_DROP`（0〜1。未設定は使わない）を設定すると、公開中の版がある業務 Agent は、その下書きの評価と同じ評価セット・同じ評価ケース（`cases_fingerprint`）で公開中の版を評価した最新の評価を基準にし、下書きの合格率が「基準 − この値」より低ければ 409（基準の評価が無ければ、公開中の版を評価するよう案内して 409。初めての公開は対象外）。どちらかの門を使うときは、`AGENT_PUBLISH_MIN_CASES`（0 は求めない）で下書きの評価の合格率の分母のケースの数（`summary.judged_cases`）の下限も見る。既定はすべて使わない。閾値は、基線（今の版の合格率とそのぶれ）を測ってから決める。変更の無い再公開・前の版に戻す・スキルの更新だけの公開は対象にしない。
  - 版の履歴は直近 `AGENT_MAX_PUBLISHED_VERSIONS`（既定 5）版だけを残す。公開のときに古い版から消し（公開中の版・スキルの更新の取り込みの前の公開中の版は残す。番号は振り直さない。規則は `pr_backend_core.version_retention`）、Run・評価の記録は版の番号だけを持つので消えない。消えた版で続ける Run は公開中の版で動く（#2356）。
  - 公開の版は使うスキルの中身（指示・説明・MCP のツール・中身の hash）を固定し、公開中の版の Run は固定した中身で動く（下書きは今のスキル。組み込みのスキルも固定する）。スキルが変わった業務 Agent は「スキルが更新されています」を出し、再公開で取り込む（#1855）。
- Run は Agent を選ぶだけで作れる（Binding の選択は無い）。開発は API のプロセスで実行（`in_process`）し、本番は Oracle checkpoint の row lock と Run lease を使う runtime-dispatcher（`python -m app.features.agent.runtime_dispatcher`）。
  - 実行の開始は、queued の Run を今の claim（lease の所有者・token・世代と期限）に対して 1 回だけ受け付ける条件付きの遷移にし、実行の区切りの書き戻しは実行の世代（token）で守る（`run_claim`。#2606。詳細は設計 §5）。開始の入口（dispatcher・in_process・承認の後の再開）を足すときもこの契約を通す。
- 保存先: Run・業務 Agent と、画面・API で変えた定義（Skill・MCP 接続・ツール権限）は `AGENT_RUNTIME_REPOSITORY_BACKEND` に従い、Oracle では共通の `PLATFORM_ORACLE_*` で `AGENT_RUNTIME_*` / `AGENT_CONTROL_PLANE_ITEMS` に保存する（テーブルはシステムテーブルが作る。アプリは DDL を実行しない）。
  - MCP 接続の秘密は `app.secret_box` で暗号化する。`.env` の宣言は保存しない。
  - 既定の `auto` は、DB の設定がそろっていれば `oracle_checkpoint`、無ければ `memory`（保存しない。再起動で消える）を起動時に選ぶ（`storage_backend`）。保存先の 1 件の Run の不整合では起動を止めず、Run 単位で直す・退避する（`quarantined`）。
  - DB に接続できないあいだも起動し（Run の repository は import 時に作らない）、接続できた時点で再起動せずに読み込む（`storage_bootstrap`。それまで業務の API は 503。docs/agent-control-plane-design.md §5.1.1）。
  - Oracle は Run・業務 Agent を 1 件 = 1 行（`AGENT_RUNTIME_ENTITIES`）で持ち、変えたものだけを書く（#1939。migration 016 が以前の全体の snapshot を移す）。repository の変更は変えた Run・業務 Agent に印を付け（`_mark_run_changed` など。Event を足すと自動）、書き込みは lock を放した後に行う（event loop の上の変更はバックグラウンドで書く）。`router.py` の `async def` の handler は repository を `run_in_threadpool` で呼ぶ（`tests/test_router_threadpool.py`）。
  - 書くプロセスは head（`AGENT_RUNTIME_CHECKPOINTS` の行の版 `VERSION_NO`・`WRITER_ID`）を lock し、前回の同期の後に別のプロセスが書いた行を取り込んでから書く（`runtime_checkpoint_store`。#1916。片方の書き込みがもう片方の更新を消さない。両方が同じ 1 件を変えたときだけ後の値）。
  - テストは `tests/conftest.py` で memory に固定する。保存先の状態は `GET /api/runtime/storage` が返し、「実行環境」の「保存先」のカードと、定義・実行を作る画面の先頭の `NonPersistentStorageNotice`（warning の Banner）が案内する。
- 利用状況・フィードバックの集計: Oracle の構成は Run の事実（`AGENT_RUN_FACTS`。1 Run = 1 行）を SQL で集計する（期間は 365 日まで。書き込みは `run_facts_store` のバックグラウンドの MERGE で、Run を止めない）。memory / file はメモリの Run を集計する（docs/agent-control-plane-design.md §5.2）。
- Agent はスキルだけを持つ。プラグイン（Skill・MCP・resource の配布単位）とマーケットプレイスは作り直さない（#1798・#1803 で削除）。スキルはビルトイン・`AGENT_SKILLS_DIR` の SKILL.md・`AGENT_SKILLS_DEFINITIONS_JSON`・画面で入れ、MCP 接続は直接登録する。
- SKILL.md と同じディレクトリの `references/` の下のテキスト / Markdown は、スキルの参照文書（`references`）として読む（Agent Skills の progressive disclosure。symlink を辿らず、ディレクトリの外を読まず、件数・大きさに上限）。指示には索引だけを入れ、その Run にだけ `skill_reference_read` を渡して必要な部分を読む。読み取りも `tool_registry.invoke` のポリシー・監査を通す。`scripts/` などの同梱のコードは読まず・実行しない。
- スキルは Agent Skills の形（`<name>/SKILL.md` ＋ `references/` の zip）で書き出せる（`GET /api/skills/{skill_id}/export`・画面のスキルの操作・MCP の `agent_skill_export`。`features/agent/skill_export.py`。#1874）。組み込みのスキルは `skills/` と同じ内容、画面のスキルは frontmatter を作り、`skills-ref` の検証を通す。
- Prompt / Workflow / Template は非実行・版管理 resource。独立 Workflow engine を作らない。

## Skill と実行の五つの不変条件（#3240）

業務 Agent・スキル・ツール・実行の正本。依存は `業務 Agent → スキル → ツール（MCP / Control Plane）` の一方向で、実行は組み込み Runtime。各条件を守るコードとテストを並べる（テストは `agent/backend/tests/`）。

1. **能力の宣言はスキル経由。宣言は認可ではない。**
   - 業務 Agent はツールの項目を持たない。使うツールは `skill_ids` で選んだスキルの `mcp_requirements` の和集合だけ（`builtin_runtime.agent_mcp_requirements` / `agent_tool_names`）。作成・変更の API はツールの直接の指定（`runtime.DIRECT_TOOL_FIELDS`: `tool_names`・`mcp_requirements`）を 422 で拒み、保存済みの旧い値（`tool_names`・`migration_required`）は読むときに無視する（未リリースのため互換を持たない）。
   - 宣言はモデルに渡す候補を決めるだけ。ツール権限（「承認」「拒否」）・承認・呼び先のデータの認可は、実行の直前に別に効く。
   - ツールの層（`tools.py` とそこから import する module）は、スキル・業務 Agent・Run の module を import しない。Run の終わりの判定は上の層が差し込む（`tools.set_run_finished_check`）。スキルの一覧のツール `agent_skill_list` は、この向きに反するので削除した。
   - スキルの追加・変更・削除は、ツールの登録・MCP 接続・ツール権限を変えない。使用中のスキルの削除を 409 にするのは上の層の整合の検査で、下の層からの逆依存ではない。
   - 実行環境が足すツールは能力の宣言ではない。`skill_reference_read`（その Run のスキルの参照文書）・`ask_user_confirmation`・`record_output`（業務 Agent の出力の項目）・会話の添付を読むツールは、Run 自身の業務 Agent・スキル・会話から組み込み Runtime が作り、外部の MCP のツールを足さない。Control Plane が自分で呼ぶ後始末（`nl2sql_cancel_job` など）と業務ガイドの照合も、モデルに渡すツールを広げない。どれも `tool_registry.invoke` を通す。
   - MCP の resources / prompts は使わない（使うのは `tools/list` と `tools/call` だけ）。スキルの `references/` はスキル自身の参照文書。
   - テスト: `test_skill_tool_boundary.py`（`test_agent_api_rejects_direct_tool_fields`・`test_directly_listed_tool_is_not_exposed_to_the_run`・`test_tool_layer_does_not_import_skills`・`test_skill_changes_do_not_change_tool_provision`）、`test_builtin_runtime.py::test_instructions_and_tools_come_from_agent_and_skills`、`test_mcp_tool_safety.py::test_skill_allowlist_is_not_widened_by_a_colliding_name`、`test_skill_editor_api.py::test_skill_used_by_agent_draft_cannot_be_deleted`。
2. **明示の構成を解決できなければ、暗黙に切り替えずに失敗する。**
   - スキルが要る MCP 接続・明示したツールが 1 つでも使えなければ、ある分だけで続けず、モデルを呼ぶ前に Run を `runtime.mcp_unavailable` で失敗にする（`build_sdk_agent`）。省略時の既定（業務 Agent の `model_id` が空なら既定のテキストモデル）を選ぶことは切り替えではない。
   - テスト: `test_builtin_runtime.py::test_partially_unavailable_required_mcp_stops_before_the_model`、`test_skill_tool_boundary.py::test_missing_required_tool_fails_the_run_instead_of_intersecting`、`test_product_mcp.py::test_missing_user_fails_before_calling_without_a_substitute`。
   - モデル: platform の `enterprise_ai_connection_for_model` は、登録に無い `model_id`（登録を消したモデルなど）をプライマリ接続へ切り替えずに `EnterpriseAiModelNotRegisteredError` にする。空の ID は既定のテキストモデルの接続、既定のテキスト / Vision モデルの ID は登録モデルに無くてもプライマリ接続（`.env` だけの構成）。組み込み Runtime はこれを `runtime.model_not_registered` の Run の失敗にする（`test_builtin_runtime.py::test_unregistered_model_fails_without_switching_connections`、`platform/packages/system_settings_backend/tests/test_model_connections.py`）。
3. **未対応の操作を成功にしない。**
   - REST は状態に合う status と code を返す（ツールの直接の指定は 422）。MCP は契約の tool error / JSON-RPC の error を返す（未対応の method は `METHOD_NOT_FOUND`）。Run は失敗の状態と `error_code` で表す。
   - モデルに渡していない名前のツールの呼び出しは実行せず、呼べる名前をモデルへ返し、理由を残す（下の「宣言の外の呼び出しの監査」）。表示用の任意の項目は、契約で任意としたものだけ省いてよい。
   - テスト: `test_skill_tool_boundary.py::test_undeclared_calls_are_recorded_with_reasons_and_not_run`、`platform/packages/backend_core/tests/test_mcp.py`、`test_tool_call_boundary.py::test_invalid_arguments_never_reach_the_handler`。
4. **認可は検証済みの認証文脈から決める。**
   - 身元・ロール・対象範囲は、Cookie のセッション・API キー・サービストークンと、サーバーの権限の情報から決める。request の本文の利用者・ロール・承認者は使わない（承認の `decided_by` は Cookie の利用者）。
   - サービストークンの `sub` は Run の利用者（承認の後も承認者にしない）。モデルの引数で `sub`・データの範囲の claim は変わらない。共通の認証の詳細は [backend-standard.md](../../../platform/docs/backend-standard.md) と [mcp.md](./mcp.md)。
   - テスト: `test_security_scope.py::test_approval_is_scoped_and_decided_by_principal`、`test_agent_data_scope.py::test_model_arguments_do_not_change_the_token_subject_or_scope`、`test_builtin_runtime.py::test_mcp_tool_after_approval_uses_the_run_creator_token`。
5. **検索の本文・ツールの結果・モデルの出力は、権限・承認の根拠にしない。**
   - これらは操作と引数の提案にだけ使う。実行の直前のツール権限の判定と、承認の記録の照合は迂回できない。承認は Run・呼び出し ID・ツール・引数に結び付き、再開でも最新のツール権限を確かめる。
   - 承認の要否の根拠にする annotations は、注釈を信頼する接続の `tools/list` の定義だけ。ツールの結果の中の annotations・「承認済み」・権限の変更の要求は使わない。
   - テスト: `test_untrusted_content_authority.py`（合成の「承認済み」・権限の変更の要求・偽の annotations・偽の承認の ID を検索とツールの結果に入れ、その文言に従って書き込みを求める台本）、`test_tool_call_boundary.py::test_approval_is_bound_to_the_call_id_and_arguments` / `test_policy_changed_to_ask_after_build_waits_for_approval` / `test_policy_changed_to_deny_after_approval_is_not_executed`、`test_tool_policy_catalog.py::test_read_only_hint_is_used_only_for_trusted_connections`、`test_builtin_runtime.py::test_mcp_run_does_not_wait_for_approval`。

### 接続のすべてのツールの露出（棚卸し）

- 接続のすべてを渡すのは、`mcp_requirements` の `tool_names` が空の宣言だけ（SKILL.md の `production-ready-mcp-tools` に `<接続>` だけを書くか、画面の JSON で空にする）。組み込みのスキルは明示の一覧だけを使う（`test_builtin_skills_declare_explicit_tool_lists`）。接続のすべてを使うのは、利用者が作ったスキルだけ。
- そのスキルを使う業務 Agent には、接続に後から足したツールも渡る。公開の版も同じ（版が固定するのはスキルの中身で、接続のツールの一覧ではない）。
- 承認の要否は、呼び出しごとに今のツール権限で決まる。書き込みのツール（`readOnlyHint` が無いか false）と、注釈を信頼しない接続のツールは既定で「承認」。「拒否」のツールは渡さない（`test_whole_connection_exposes_added_tools_under_the_current_policy`）。
- 外部の接続にツールを足すときは、管理者がツール権限を見直す。露出を固定したいスキルは明示の一覧にする。既存のスキルを黙って明示の一覧に書き換えることはしない。
- 明示の一覧は、一覧に無いツールを渡さない。一覧のツールが接続に無ければ、交差集合で隠さずに Run を失敗にする（不変条件 2）。

### 宣言の外の呼び出しの監査

モデルが渡していない名前のツールを呼んだら、SDK の `tool_not_found` で Run を失敗にせず、呼べる名前をモデルへ返す（#1303）。呼び出しは実行しない。呼び出しごとに理由を決め、Run のイベント（`payload.undeclared_tool_call`: `tool_name`・`call_id`・`reason`。`severity=warning`）・ログ `builtin_runtime_undeclared_tool_call`・metric `agent_undeclared_tool_calls_total{reason}` に残す（`builtin_runtime.record_undeclared_call`）。検知を理由に、渡すツールを広げない。

| reason | 意味 |
|---|---|
| `name_mismatch` | 渡したツールを別の名前（素の名前・別の接頭辞）で呼んだ（モデルの名前違い） |
| `policy_denied` | スキルが宣言したが、ツール権限が「拒否」で渡していない |
| `declined` | スキルが宣言したが、この Run で利用者が確認を取り消した種類 |
| `name_conflict` | スキルが宣言したが、別の出自のツールと名前が重なった（#2596） |
| `previous_context` | 同じ会話の前の Run で呼んだが、この Run では渡していない（前の文脈） |
| `not_declared` | 登録済みの接続・Control Plane のツールの名前だが、業務 Agent のスキルが宣言していない（スキルの設定の不足か、宣言の外への試み） |
| `unknown` | どの接続・ツールの名前にも当たらない |

### 保存・公開・実行の就緒条件

保存できることは、実行できることを意味しない。段階ごとに確かめることを分ける。

| 段階 | 確かめること | 確かめないこと（後の段階で明示の失敗にする） |
|---|---|---|
| 保存（下書きの作成・変更） | 名前・ID の形、スキルが登録済み（400）、データの範囲を編集者が使える（`_verify_data_scopes`）、出力の項目の形、業種テンプレートが存在する、ツールの直接の指定が無い（422） | スキルが要る MCP 接続・ツールが今使えるか、モデルが登録済みか |
| 公開 | スキルが登録済み、スキルの中身の固定（`pin_skills`）、設定したときだけ評価の門（`AGENT_PUBLISH_MIN_PASS_RATE`・回帰の門） | 保存と同じ（公開の後にも接続・モデル・ツール権限は変わる） |
| 実行（Run の開始と各呼び出し） | 業務 Agent が有効で公開した版がある（下書きは管理者と持ち主だけ）、利用者の対象範囲、モデルと接続の解決（`runtime.model_not_configured` など）、要る MCP 接続と明示したツールがそろう（`runtime.mcp_unavailable`）、呼び出しごとの取消・引数・最新のツール権限・承認の照合、呼び先の認可（サービストークンの `sub` は Run の利用者） | — |

業務 Agent の一括の import は持たない。保存先の読み込みは 1 件の不整合を退避し（#853）、組み込みの業務 Agent を足す。スキルの読み込み（`AGENT_SKILLS_DIR`・`AGENT_SKILLS_DEFINITIONS_JSON`・画面）は形を検証する。

### #2586（出荷前の横断の検証）との対応

| #2586 の障害の条件 | 不変条件 | 確かめるテスト |
|---|---|---|
| 検索の結果の「承認不要」などの注入 | 5 | `test_untrusted_content_authority.py`（危険な次の操作を求める台本） |
| 権限外の操作・未承認の書き込み | 1・5 | `test_skill_tool_boundary.py`、`test_tool_call_boundary.py`（#2610）、`test_product_mcp.py::test_write_tools_require_approval_by_default` |
| 承認の後の利用者の委譲 | 4 | `test_builtin_runtime.py::test_mcp_tool_after_approval_uses_the_run_creator_token` |
| RAG の 503・timeout・接続の失敗 | 2・3 | `test_product_mcp.py`（再試行・書き込みを再送しない）、`test_builtin_runtime.py::test_unreachable_mcp_service_is_a_tool_error_and_the_run_continues` |
| 必須の接続・ツールの不足 | 2 | `test_partially_unavailable_required_mcp_stops_before_the_model`、`test_missing_required_tool_fails_the_run_instead_of_intersecting` |
| 取消の後の新しい外部の操作 | 3 | `test_tool_call_boundary.py::test_cancel_during_a_read_stops_the_following_write` / `test_approved_call_is_not_run_after_cancel`（#2609） |
| 二重の実行（古い所有者の開始） | 3 | `test_run_claim_fencing.py`（#2606） |

不変条件では扱わず #2586 に残すもの: 停止と復帰の手順（受付の停止・特定の能力の禁止・緊急の停止）とその演練、点検の証拠の集約（pass / blocker / warning / not-applicable）、承認の滞留の責任者、request / run / tool の ID の関連付けの測定、実環境のモデルの API / Oracle の隔離環境での確認。
