# 表示用 trace summary

正本は `pr_backend_core.observability.tracing.TraceSummary`。`trace-summary.schema.json` はその JSON Schema で、共通 backend のテストが一致を検査する。現在の `schema_version` は 1。製品は既存の Run / job / 実行履歴の認可を通して返し、UI は `traceTimeline` で検証して描画する。

- OTel の trace ID / span ID と、業務の operation ID は別にする。
- `spans` は実行時に計測した安全な項目だけ。`attributes` は共通の allowlist に限り、本文・SQL・引数・認証・結果の行を含めない。
- `completeness` は complete / partial / unavailable。上限や未終了の子があれば partial、計測を無効にした場合は unavailable とし、架空の span を作らない。
- `sampled` は外部送信の抽出判定で、送信済みの保証ではない。画面用の記録はこの抽出判定とは独立して保持する。
- キャッシュは入力、推論は出力の内数。不明は項目を持たず、0 と区別する。
- 承認後の再開・job の再試行は複数の summary を `trace_segments` に保持する。各区間の実行時間を表示し、人の操作待ちを LLM 時間に足さない。
- 古い進捗・所要時間しかない履歴を、この実測 DTO として再建しない。UI の時間の線は再構成と明記する。

外部の trace は標準 OTLP/HTTP で collector へ送る。この DTO はブラウザ用の安全な投影であり、OTLP の代替ではない。

呼び出しの `display.details.call.capture_enabled=false` は、開始時の運用設定で本文の記録を止めたことを表す。input / output / schema は未記録の空の値にし、通常の未記録と画面で区別する。値が無い旧記録は記録制限の追加前の契約として読む。
