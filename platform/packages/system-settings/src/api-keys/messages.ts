/**
 * API キーの画面の既定の文言（3 製品共通。#1805・#1874）。
 * 製品は `messages` で一部を上書きできる（同じ key）。
 */
export const API_KEYS_MESSAGES = {
  // 自分のキー（アカウントの画面の「API キー」のタブ。#1873）
  "apiKeys.endpoint.title": "接続先",
  "apiKeys.endpoint.description":
    "MCP クライアントに接続先の URL を登録し、Authorization ヘッダーに「Bearer <API キー>」を付けます。キーはあなたとして動き、あなたのロールの権限とデータの範囲で答えます。",
  "apiKeys.endpoint.copy": "URL をコピー",
  "apiKeys.endpoint.copied": "URL をコピーしました",
  "apiKeys.endpoint.copyFailed": "コピーできませんでした。URL を選択してコピーしてください。",
  "apiKeys.endpoint.local": "この画面の URL",
  "apiKeys.endpoint.public": "公開の URL（OCI）",
  "apiKeys.endpoint.same": "この画面の URL（公開の URL と同じ）",
  "apiKeys.endpoint.copyLabel": "{product}の{kind}: URL をコピー",
  "apiKeys.endpoint.caNote":
    "https の URL が配備の自作の CA の証明書のときは、MCP クライアントにも CA を信頼させます（Claude Code は例の NODE_EXTRA_CA_CERTS）。CA の証明書:",
  "apiKeys.endpoint.example": "Claude Code の登録の例",
  "apiKeys.endpoint.exampleNote":
    "PR_API_KEY に発行したキーを入れます。--header は単一引用符のまま登録し、キーの値を設定ファイルに書かせません。MCP のサーバー名は組み込みのスキルに合わせて rag / nl2sql にします。",
  "apiKeys.example.keyComment": "発行したキー（シェルの起動ファイルや secret の管理に置き、repo に書かない）",
  "apiKeys.example.timeoutComment": "MCP のツールの待ち時間（ミリ秒）。既定の 60 秒では rag_search が切れる",
  "apiKeys.example.caComment": "自作の CA の証明書（{url} を保存したファイル）",
  "apiKeys.create.title": "API キーを発行",
  "apiKeys.create.description":
    "キーは端末・用途ごとに分けて発行します（1 人 {max} 件まで）。秘密は発行の直後に 1 回だけ表示します。",
  "apiKeys.create.name": "名前（用途）",
  "apiKeys.create.namePlaceholder": "例: 会社の PC の Claude Code",
  "apiKeys.create.nameRequired": "名前を入力してください。",
  "apiKeys.create.products": "使える製品",
  "apiKeys.create.productsRequired": "使える製品を 1 つ以上選んでください。",
  "apiKeys.create.agentScope": "使える業務 Agent",
  "apiKeys.create.agentScope.all": "すべての業務 Agent",
  "apiKeys.create.agentScope.selected": "選んだ業務 Agent だけ",
  "apiKeys.create.agentScope.hint":
    "キーで呼べる業務 Agent を絞ると、キーが漏れたときの影響を小さくできます。あなたが使えない業務 Agent は選んでも使えません。",
  "apiKeys.create.agentScope.required": "業務 Agent を 1 つ以上選んでください。",
  "apiKeys.create.agentScope.empty": "選べる業務 Agent がありません。",
  "apiKeys.create.agentScope.elsewhere":
    "このキーは、あなたが使えるすべての業務 Agent を呼べます。業務 Agent を絞るときは、Agent の画面で発行します。",
  "apiKeys.create.expiry": "有効期限",
  "apiKeys.create.expiry.30": "30 日",
  "apiKeys.create.expiry.90": "90 日",
  "apiKeys.create.expiry.365": "365 日",
  "apiKeys.create.expiry.none": "無期限",
  "apiKeys.create.submit": "発行",
  "apiKeys.create.limit":
    "API キーは 1 人 {max} 件までです。使わないキーを削除してから発行してください。",
  "apiKeys.created.title": "API キーを発行しました",
  "apiKeys.created.description":
    "このキーは今だけ表示します。コピーして MCP クライアントの設定（環境変数など）に保管してください。閉じると二度と表示できません。",
  "apiKeys.created.label": "API キー",
  "apiKeys.created.copy": "コピー",
  "apiKeys.created.copied": "API キーをコピーしました",
  "apiKeys.created.copyFailed": "コピーできませんでした。欄の文字を選択してコピーしてください。",
  "apiKeys.created.done": "保管しました",
  "apiKeys.list.mine": "あなたの API キー",
  "apiKeys.list.all": "全員の API キー",
  "apiKeys.list.label": "API キーの一覧",
  "apiKeys.list.loading": "API キーを読み込んでいます",
  "apiKeys.list.loadFailed": "API キーを読み込めませんでした。",
  "apiKeys.list.empty": "API キーはまだありません。",
  "apiKeys.list.emptyHint": "上の「API キーを発行」から、MCP クライアントごとにキーを発行します。",
  "apiKeys.list.emptyAll": "API キーはまだありません。",
  "apiKeys.list.emptyAllHint": "各利用者が、アカウントの画面の「API キー」から自分のキーを発行します。",
  "apiKeys.list.noMatch": "条件に合う API キーはありません。",
  "apiKeys.list.search": "名前・利用者で絞り込み",
  "apiKeys.list.clearSearch": "検索語をクリア",
  "apiKeys.list.count": "{count} 件",
  "apiKeys.list.pagerLabel": "API キー一覧のページ",
  "apiKeys.column.name": "名前",
  "apiKeys.column.products": "使える製品",
  "apiKeys.column.expires": "有効期限",
  "apiKeys.column.lastUsed": "最後に使った日時",
  "apiKeys.column.created": "発行日時",
  "apiKeys.column.owner": "利用者",
  "apiKeys.column.actions": "操作",
  "apiKeys.noExpiry": "無期限",
  "apiKeys.expired": "期限切れ",
  "apiKeys.neverUsed": "未使用",
  "apiKeys.deletedUser": "削除されたユーザー",
  "apiKeys.revoke": "削除",
  "apiKeys.rowActions": "{name} の操作",
  "apiKeys.revokeTitle": "「{name}」を削除しますか",
  "apiKeys.revokeDescription":
    "キーはすぐに使えなくなり、元に戻せません。このキーを使っている MCP クライアントは RAG・NL2SQL・Agent を呼べなくなります。",
  "apiKeys.revokeOwnerDescription":
    "{owner} さんのキーです。キーはすぐに使えなくなり、元に戻せません。このキーを使っている MCP クライアントは RAG・NL2SQL・Agent を呼べなくなります。",
  "apiKeys.revoked": "API キーを削除しました",
  "apiKeys.operationFailed": "操作できませんでした。",
  "apiKeys.retry": "再試行",
  "apiKeys.admin.title": "API キー",
  "apiKeys.admin.subtitle":
    "全員の API キーを確かめ、不要なキーや漏れたおそれのあるキーを削除します。キーの発行は各利用者がアカウントの画面から行います。",
  "apiKeys.product.rag": "RAG",
  "apiKeys.product.nl2sql": "NL2SQL",
  "apiKeys.product.agent": "Agent",
  "apiKeys.scope.count": "{product}（{count} 件に限定）",
} as const;

export type ApiKeysMessageKey = keyof typeof API_KEYS_MESSAGES;
export type ApiKeysMessages = Partial<Record<ApiKeysMessageKey, string>>;

/** 製品の上書きを合わせた文言の関数（`{name}` を params で置き換える）。 */
export function apiKeysTranslator(messages?: ApiKeysMessages) {
  return (key: ApiKeysMessageKey, params?: Record<string, string | number>): string => {
    let value: string = messages?.[key] ?? API_KEYS_MESSAGES[key];
    if (params) {
      for (const [name, replacement] of Object.entries(params)) {
        value = value.replace(new RegExp(`\\{${name}\\}`, "g"), String(replacement));
      }
    }
    return value;
  };
}
