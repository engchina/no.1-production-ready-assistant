# NL2SQL のオントロジーの標準

> nl2sql/AGENTS.md から移した詳細（#2276）。nl2sql/AGENTS.md の規則と同じ強さで守る。規則を変えるときはこのファイルを直し、AGENTS.md には規則の要約と案内だけを書く。

## オントロジーの標準（#1729）

詳細・対応表は [docs/profile-ontology-capabilities.md](../../docs/profile-ontology-capabilities.md)「オントロジーの標準」。生成・検証・Markdown・画面はこの規則と食い違わせない。

- **中身は業界の標準に沿い、専門性を保つ。** 基準は Palantir Foundry の Ontology（オブジェクト型（Object Type）・プロパティ（Property）・共有プロパティ（Shared Property）・リンク型（Link Type）・値型（Value Type）など）。
  - オブジェクト型: 表示名・API 名（snake_case）・説明・主キー（Primary Key）・タイトルのプロパティ（Title Property）・対応する表。
  - プロパティ: 業務の型（Base Type）と精度・桁を含む物理の型（Physical Type）・必須・単位・説明・所属のオブジェクト型。複数のオブジェクト型で同じ意味のものは共有プロパティ。
  - リンク型: 両端のオブジェクト型・多重度（Cardinality）・結合のキー（外部キー）・それぞれの向きの名前。
  - 用語は SKOS の考え方（優先ラベル（prefLabel）＝表示名・別ラベル（altLabel）＝別名・定義（definition）＝説明）。
  - 指標（Metric）は semantic layer の書き方（測定値・集計方法・ディメンション・粒度・時間のディメンション・絞り込みの条件）。
- **記述は日本語が主。** 専門用語は「日本語（英語）」で併記し、訳は `backend/app/features/nl2sql/ontology_terms.json` の 1 つの対応表に置く（Markdown の生成と画面の i18n の `ontologyResults.kind.*` / `ontologyResults.field.*` が同じ訳を使い、`backend/tests/test_nl2sql_ontology_unify_links.py` の `test_terms_are_shared_with_the_frontend_i18n` が一致を確かめる）。物理の名前（表・列・API 名・SQL）は英語のまま。
- **画面の説明・診断は平易な日本語**にし、各診断は「何が問題か（業務の名前）」と「どうすればよいか」の 2 文にする。内部の ID・項目名・診断コードは「技術的な詳細」に畳む。平易さのためにオントロジーの中身の構造・専門性は崩さない。
- **生成は best effort**（無い項目は空でよい。推測で埋めない）。統合の根拠は物理の表・列（リンク型は両端と結合のキー）で、名前・種類の名前の ID では併合しない。
- **LLM に渡す Markdown（`render_llm_context`）は業務の知識だけ**（#1720）。
