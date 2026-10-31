/** ブラウザの診断も JST JSON Lines。業務本文・Error.message / stack は出力しない。 */
export interface BrowserDiagnostic {
  event: "card_render_failed" | "unsaved_changes_blocker_missing" | "deferred_styles_failed";
  serviceName: "production-ready-rag" | "production-ready-nl2sql" | "production-ready-agent" | "production-ready-platform";
  requestId?: string;
  error?: unknown;
}

const MESSAGES = {
  card_render_failed: "カードの描画に失敗しました",
  unsaved_changes_blocker_missing: "未保存の変更を確認する blocker が配置されていません",
  deferred_styles_failed: "最初の描画の後に読む CSS の読み込みに失敗しました",
} as const;
const REQUEST_ID = /^[A-Za-z0-9._:-]{1,128}$/;
const ERROR_TYPES = new Set(["Error", "TypeError", "RangeError", "SyntaxError", "ReferenceError", "ApiError"]);

/** epoch を同じ瞬間の JST 表記へ変換する。ホスト・ブラウザの timezone に依存しない。 */
export function diagnosticTimestamp(at = new Date()): string {
  return new Date(at.getTime() + 9 * 60 * 60 * 1000).toISOString().replace("Z", "+09:00");
}

export function logBrowserDiagnostic(level: "WARNING" | "ERROR", diagnostic: BrowserDiagnostic): void {
  // 診断の失敗で元の描画や離脱ガードを壊さない。
  try {
    const requestId = diagnostic.requestId ?? (
      diagnostic.error && typeof diagnostic.error === "object"
        ? Object.getOwnPropertyDescriptor(diagnostic.error, "requestId")?.value as unknown
        : undefined
    );
    const errorType = diagnostic.error instanceof Error
      ? (ERROR_TYPES.has(diagnostic.error.name) ? diagnostic.error.name : "Error")
      : undefined;
    const wire = JSON.stringify({
      schema_version: 1, timestamp: diagnosticTimestamp(), level,
      name: "browser", event: diagnostic.event, message: MESSAGES[diagnostic.event],
      service_name: diagnostic.serviceName, component: "frontend",
      ...(typeof requestId === "string" && REQUEST_ID.test(requestId) ? { request_id: requestId } : {}),
      ...(errorType ? { exception_type: errorType } : {}),
    });
    if (level === "ERROR") console.error(wire);
    else console.warn(wire);
  } catch { /* 診断に任意の Error 原文を fallback として出さない。 */ }
}
