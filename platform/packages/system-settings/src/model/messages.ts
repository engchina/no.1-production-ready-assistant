/**
 * モデル設定画面の既定の文言（NL2SQL の文言。#103）。
 * key は NL2SQL の i18n key と同じにして、移設したコードをそのまま使えるようにしている。
 */
export const MODEL_MESSAGES = {
  "common.delete": "削除",
  "settings.model.connection.add": "{connection}を設定",
  "settings.model.connection.error.apiKeyRequired": "API key を入力してください。",
  "settings.model.connection.error.endpointRequired":
    "Endpoint URL を入力してください。",
  "settings.model.connection.error.projectRequired":
    "Project OCID を入力してください。",
  "settings.model.connection.error.missing":
    "「{model}」の接続がありません。登録されている接続から選び直してください。",
  "settings.model.connection.error.unsaved":
    "{connection} はまだ保存されていません。上の「OCI Enterprise AI」で接続を保存してから、登録モデルを保存してください。",
  "settings.model.connection.primary": "プライマリ接続",
  "settings.model.connection.primaryDescription":
    "登録モデルで接続を選ばなければ、プライマリ接続を使います。",
  "settings.model.connection.remove": "{connection}を削除",
  "settings.model.connection.removeConfirm.description":
    "{connection}の入力内容を削除します。保存するまで確定しません。",
  "settings.model.connection.removeConfirm.descriptionInUse":
    "{connection}を使っている登録モデルがあります（{models}）。削除すると、これらのモデルはプライマリ接続を使います。保存するまで確定しません。",
  "settings.model.connection.removeConfirm.moveAndRemove":
    "プライマリ接続に移して削除",
  "settings.model.connection.removeConfirm.title":
    "{connection}を削除しますか？",
  "settings.model.connection.secondary": "セカンダリ接続",
  "settings.model.connection.secondaryDescription":
    "登録モデルの「接続」でセカンダリ接続を選んだモデルは、この接続で呼び出します。",
  "settings.model.connection.secondaryEmpty.hint":
    "別のリージョンや Project にあるモデルを使うときに設定します。設定すると、Endpoint URL・Project OCID・API key はすべて必須です。",
  "settings.model.connection.secondaryEmpty.title":
    "セカンダリ接続は設定されていません。",
  "settings.model.connection.tabs": "OCI Enterprise AI の接続",
  "settings.model.connection.tertiary": "ターシャリ接続",
  "settings.model.connection.tertiaryDescription":
    "登録モデルの「接続」でターシャリ接続を選んだモデルは、この接続で呼び出します。OpenAI や OpenAI 互換 API のモデルにも使えます。",
  "settings.model.connection.tertiaryEmpty.hint":
    "OpenAI や OpenAI 互換 API（OCI Enterprise AI も可）のモデルを使うときに設定します。設定すると、Endpoint URL と API key は必須です。Project OCID は OCI Enterprise AI を使うときだけ入力します。",
  "settings.model.connection.tertiaryEmpty.title":
    "ターシャリ接続は設定されていません。",
  "settings.model.connection.unsaved": "未保存",
  "settings.model.defaults.description":
    "処理の種類ごとに使うモデルを登録モデルから選びます。",
  "settings.model.defaults.error.judgeRemoved":
    "「{model}」は登録モデルにありません。登録モデルから選び直してください。",
  "settings.model.defaults.error.noVisionModel":
    "画像入力に対応したモデルがありません。登録モデルの 1 つ以上で「画像入力に対応」をオンにしてください。",
  "settings.model.defaults.error.textRemoved":
    "「{model}」は登録モデルにありません。登録モデルから選び直してください。",
  "settings.model.defaults.error.textRequired":
    "既定のテキストモデルを選択してください。",
  "settings.model.defaults.error.visionNotCapable":
    "「{model}」は画像入力に対応していません。対応をオンにするか、別のモデルを選んでください。",
  "settings.model.defaults.error.visionRemoved":
    "「{model}」は登録モデルにありません。登録モデルから選び直してください。",
  "settings.model.defaults.error.visionRequired":
    "既定の画像対応モデルを選択してください。",
  "settings.model.defaults.judge": "既定の判定モデル",
  "settings.model.defaults.judgeDefaultText": "既定のテキストモデル",
  "settings.model.defaults.judgeHelp":
    "RAG の品質評価の判定に使います。自分の回答は高く判定されやすいため、モデルの比較では別のモデルに固定してください。",
  "settings.model.defaults.judgeSameAsAnswer": "回答と同じモデル",
  "settings.model.defaults.placeholder": "モデルを選んでください",
  "settings.model.defaults.text": "既定のテキストモデル",
  "settings.model.defaults.textHelp":
    "画像を扱わない処理（回答生成・要約・SQL 生成など）で使います。",
  "settings.model.defaults.title": "既定のモデル",
  "settings.model.defaults.vision": "既定の画像対応モデル",
  "settings.model.defaults.visionHelp":
    "画像を読み取る処理（文書解析の図・画像の読み取りなど）で使います。画像入力に対応したモデルだけを選べます。",
  "settings.model.actions.label": "{section} の操作",
  "settings.model.enterprise.addModel": "追加",
  "settings.model.enterprise.apiKey": "API key",
  "settings.model.enterprise.apiKeyHelp":
    "OpenAI-compatible gateway の Bearer 認証で使います。保存先はサーバーの .env のみで、JSON や API 応答には含めません。",
  "settings.model.enterprise.apiKeyHide": "API key を隠す",
  "settings.model.enterprise.apiKeyNotSet": "未設定",
  "settings.model.enterprise.apiKeySaved": "保存済み",
  "settings.model.enterprise.apiKeyShow": "API key を表示",
  "settings.model.enterprise.clearApiKey": "保存済み API key を削除する",
  "settings.model.enterprise.connection": "接続",
  "settings.model.enterprise.connectionOfModel": "モデル {number} の接続",
  "settings.model.enterprise.description":
    "回答生成と画像・OCR の解析に使う Enterprise AI の接続情報を設定します。プライマリ接続に加えてセカンダリ接続と、OpenAI や OpenAI 互換 API にも使えるターシャリ接続を設定でき、登録モデルごとに使う接続を選べます。",
  "settings.model.enterprise.displayName": "表示名",
  "settings.model.enterprise.endpoint": "Endpoint URL",
  "settings.model.enterprise.endpointDocs":
    "公式ドキュメント（新しいタブで開く）",
  "settings.model.enterprise.endpointHelp":
    "公式 docs の OpenAI-compatible base URL を /openai/v1 まで指定します。入力した URL に Responses API の /responses を付けて呼び出し、path は補いません。",
  "settings.model.enterprise.endpointHelpTertiary":
    "API の base URL を path まで指定します（OpenAI は https://api.openai.com/v1、OpenAI 互換 API は https://api.deepseek.com/v1 など、OCI Enterprise AI は …/openai/v1）。入力した URL に /responses を付けて呼び出し、path は補いません。",
  "settings.model.enterprise.modelId": "モデル ID",
  "settings.model.enterprise.modelIdDuplicate":
    "モデル ID「{model}」はすでに登録されています。同じモデルは 1 行にまとめてください。",
  "settings.model.enterprise.models": "登録モデル",
  "settings.model.enterprise.modelsDescription":
    "回答生成と画像の読み取りに使うモデルを登録し、画像入力に対応するかを指定します。画像入力に対応したモデルを、画面では「画像対応モデル」（Vision 対応のモデル）と呼びます。",
  "settings.model.enterprise.modelsSaved": "登録モデルを保存しました。",
  "settings.model.enterprise.project": "Project OCID",
  "settings.model.enterprise.projectHelp":
    "OCI OpenAI-compatible API 呼び出しに必要な Generative AI project OCID。",
  "settings.model.enterprise.projectHelpTertiary":
    "OCI Enterprise AI を使うときだけ、Generative AI project OCID を入力します（入力したときだけ OpenAI-Project ヘッダーで送ります）。OpenAI や OpenAI 互換 API では空のままにします。",
  "settings.model.enterprise.parameterHelp": "推論の量・入力と出力の上限について",
  "settings.model.enterprise.modelDocs": "Oracle 公式: モデルの仕様",
  "settings.model.enterprise.apiDocs": "Oracle 公式: API の仕様",
  "settings.model.enterprise.reasoningEffort": "推論の量",
  "settings.model.enterprise.reasoningEffortOfModel": "モデル {number} の推論の量",
  "settings.model.enterprise.reasoningEffortHelp":
    "推論に対応するモデルへ、回答の生成に使う推論の程度を指定します。対応する値や効果はモデル・API により異なり、応答時間や品質を保証する設定ではありません。このシステムの none は指定を送らず、モデル側の既定を使います。選択した接続と値は「テスト」で確認してください。NL2SQL の Select AI・Select AI Agent には、この画面の推論の量は送信しません。",
  "settings.model.enterprise.reasoningEffort.none": "none（送らない）",
  "settings.model.enterprise.maxInputTokens": "入力の上限（tokens）",
  "settings.model.enterprise.maxInputTokensOfModel": "モデル {number} の入力の上限（tokens）",
  "settings.model.enterprise.maxInputTokensHelp":
    "この画面の「入力の上限」は、入力と出力を合わせたコンテキスト長を、このシステムの token 予算として設定するものです。指示・質問・会話履歴・資料などの入力と、出力のための余地を含めて扱います。選択したモデルのコンテキスト上限以下に設定してください。値を大きくしてもモデル自体の上限は増えません。出力の上限は別の設定です。",
  "settings.model.enterprise.maxInputTokensInvalid":
    "入力の上限は 4096〜2000000 の整数で入力してください。",
  "settings.model.enterprise.maxOutputTokens": "出力の上限（tokens）",
  "settings.model.enterprise.maxOutputTokensOfModel": "モデル {number} の出力の上限（tokens）",
  "settings.model.enterprise.maxOutputTokensHelp":
    "1 回の呼び出しで生成する token 数の上限です。モデル・API によっては、表示される回答だけでなく推論の token も含まれる場合があります。モデルの出力上限と、入力に使った分を除くコンテキストの残りに収まるよう設定してください。小さすぎると回答が途中で終わったり、本文が得られなかったりします。未完了時の再試行やエラーの扱いは、使用する機能によって異なります。",
  "settings.model.enterprise.maxOutputTokensInvalid":
    "出力の上限は 1〜2000000 の整数で入力してください。",
  "settings.model.enterprise.maxOutputTokensOverInput":
    "出力の上限は入力の上限（{max}）以下にしてください。",
  "settings.model.enterprise.removeConfirm.description":
    "モデル「{model}」を一覧から削除します。保存するまで確定しません。",
  "settings.model.enterprise.removeConfirm.descriptionUnnamed":
    "このモデルを一覧から削除します。保存するまで確定しません。",
  "settings.model.enterprise.removeConfirm.title": "このモデルを削除しますか？",
  "settings.model.enterprise.removeModel": "モデルを削除",
  "settings.model.enterprise.saved":
    "OCI Enterprise AI 接続設定を保存しました。",
  "settings.model.enterprise.title": "OCI Enterprise AI",
  "settings.model.enterprise.vision": "画像入力に対応",
  "settings.model.genai.description":
    "埋め込みとリランクのみ Generative AI の Cohere モデルを使います。",
  "settings.model.genai.embeddingDim": "Embedding 次元",
  "settings.model.genai.embeddingDimHelp":
    "固定値です。Cohere Embed v4 と Oracle AI Database のベクトル列に合わせます。",
  "settings.model.genai.embeddingModel": "埋め込みモデル ID",
  "settings.model.genai.rerankModel": "リランクモデル ID",
  "settings.model.genai.saved": "OCI Generative AI 設定を保存しました。",
  "settings.model.genai.title": "OCI Generative AI",
  "settings.model.legacySecret.description":
    "原因: 旧形式の model-settings.json に API Key が保存されています。復旧方法: この画面で保存すると API Key を .env へ移し、JSON から secret を削除します。移行後は接続テストを実行してください。",
  "settings.model.legacySecret.title": "旧 JSON に API Key が残っています",
  "settings.model.loadError": "モデル設定の取得に失敗しました。",
  "settings.model.loading": "モデル設定を読み込んでいます。",
  "settings.model.placeholder.apiKey": "sk-xxxxxxxxxxxxxxxx",
  "settings.model.placeholder.displayName": "業務 NL2SQL 標準",
  "settings.model.placeholder.embeddingModel": "cohere.embed-v4.0",
  // placeholder は 375px の欄にも収まる短い例（形は補足の endpointHelp。#1891）。
  "settings.model.placeholder.endpoint": "https://…oraclecloud.com/openai/v1",
  "settings.model.placeholder.endpointTertiary": "https://api.openai.com/v1",
  "settings.model.placeholder.modelId": "enterprise-llm",
  "settings.model.placeholder.project": "ocid1.generativeaiproject.oc1.…",
  "settings.model.placeholder.rerankModel": "cohere.rerank-v4.0-fast",
  "settings.model.requiredInOci": "OCI 運用時必須",
  "settings.model.save": "保存",
  "settings.model.conflict.message":
    "モデル設定は、この画面を開いた後にほかの画面（別の製品を含む）で更新されました。最新の設定を読み込んでから、保存し直してください。",
  "settings.model.conflict.reload": "最新の設定を読み込む",
  "settings.model.conflict.confirm.title": "最新の設定を読み込みますか？",
  "settings.model.conflict.confirm.description":
    "保存していない入力は破棄され、保存済みの最新のモデル設定を表示します。",
  "settings.model.conflict.confirm.action": "読み込む",
  "settings.model.conflict.reloaded": "最新のモデル設定を読み込みました。",
  "settings.model.saveError":
    "モデル設定を保存できませんでした。入力内容とバックエンド接続を確認して再試行してください。",
  "settings.model.subtitle":
    "OCI Enterprise AI の LLM カタログと OCI Generative AI（埋め込み/リランク）のモデルを設定します。",
  "settings.model.test.action": "テスト",
  "settings.model.test.apiFailed":
    "モデルテスト API の呼び出しに失敗しました。バックエンドのログとネットワークを確認してください。",
  "settings.model.test.aria": "{model} をテスト",
  "settings.model.test.failed":
    "モデルテストに失敗しました。入力値とバックエンド接続を確認してください。",
  "settings.model.test.running": "{model} をテストしています",
} as const;

export type ModelMessageKey = keyof typeof MODEL_MESSAGES;

/** `{name}` を params の値で置き換える。 */
export function t(
  key: ModelMessageKey,
  params?: Record<string, string | number>,
): string {
  let value: string = MODEL_MESSAGES[key] ?? key;
  if (params) {
    for (const [name, replacement] of Object.entries(params)) {
      value = value.replace(
        new RegExp(`\\{${name}\\}`, "g"),
        String(replacement),
      );
    }
  }
  return value;
}
