/**
 * RAG の権限コード（backend `app/security/permissions.py` の `PERMISSION_CATALOG` と同じ。#214）。
 *
 * - メニュー権限（`menu.*`）は画面の表示を許可する。ナビ項目とルートの保護に使う。
 * - capability（`rag.*`）はページ内の操作を許可する。backend は `implies` を展開済みの
 *   `permissions` を返すため、frontend は一覧に含まれるか（SYSTEM_ADMIN は常に true）だけを見る。
 * システム設定・ユーザーとロールのコードは NL2SQL と同じ（3製品共通の画面）。
 */
export const MENU_PERMISSIONS = {
  search: "menu.search",
  chat: "menu.chat",
  // 実行履歴（チャット・RAG 検索の実行の一覧と詳細。#1890）。
  history: "menu.history",
  searchAnswerProfiles: "menu.search_answer_profiles",
  evaluation: "menu.evaluation",
  feedback: "menu.feedback",
  upload: "menu.upload",
  fileList: "menu.file_list",
  knowledgeBases: "menu.knowledge_bases",
  documentCategories: "menu.document_categories",
  settingsPipeline: "menu.settings_pipeline",
  settingsPreprocess: "menu.settings_preprocess",
  settingsParserAdapters: "menu.settings_parser_adapters",
  settingsChunking: "menu.settings_chunking",
  settingsLanguagePack: "menu.settings_language_pack",
  settingsVectorIndex: "menu.settings_vector_index",
  settingsRetrieval: "menu.settings_retrieval",
  settingsPrompts: "menu.settings_prompts",
  settingsEvaluation: "menu.settings_evaluation",
  settingsEntityIndex: "menu.settings_entity_index",
  settingsGraph: "menu.settings_graph",
  settingsSystemTables: "menu.settings_system_tables",
  settingsHuggingface: "menu.settings_huggingface",
  settingsServices: "menu.settings_services",
  settingsUploadStorage: "menu.settings_upload_storage",
  settingsModel: "menu.settings_model",
  settingsDatabase: "menu.settings_database",
  settingsAnswerNotice: "menu.settings_answer_notice",
  settingsTraceRecording: "menu.settings_trace_recording",
  settingsChatAttachments: "menu.settings_chat_attachments",
  securityUsers: "menu.security_users",
  securityRoles: "menu.security_roles",
  // 全員の API キー（管理の画面）。自分のキーはメニュー権限なしで使える（#1805）。
  securityApiKeys: "menu.security_api_keys",
  securityPermissions: "menu.security_permissions",
} as const;

export const CAPABILITY_PERMISSIONS = {
  /** 検索・回答プロファイルの作成・アーカイブと、すべての検索・回答プロファイルの利用。 */
  searchAnswerProfilesManage: "rag.search_answer_profiles.manage",
  /** ナレッジベースの作成・アーカイブと、すべてのナレッジベースの利用。 */
  knowledgeBasesManage: "rag.knowledge_bases.manage",
  /** フィードバックの回答を承認済み FAQ へ反映する。 */
  feedbackManage: "rag.feedback.manage",
  /** RAG のシステムテーブルの初期化・全再作成。 */
  systemTablesManage: "rag.system_tables.manage",
  /** 品質評価の判定の校正の管理（全員の判定・一致・裁定・校正・LabelSet の入出力。#3578）。 */
  evaluationCalibrationManage: "rag.evaluation_calibration.manage",
} as const;

export type MenuPermission = (typeof MENU_PERMISSIONS)[keyof typeof MENU_PERMISSIONS];
export type CapabilityPermission =
  (typeof CAPABILITY_PERMISSIONS)[keyof typeof CAPABILITY_PERMISSIONS];
