import type { ReactNode } from "react";

import { StatusBadge } from "../data/status-badge";
import { CopyButton } from "../ui/copy-button";

/**
 * 回答の中の SQL（状態のバッジ・コピー・操作と、SQL の本文）。NL2SQL のチャット・DB 管理チャットと、Agent のチャットの
 * NL2SQL の照会の結果で同じ部品を使う（#2264・#2346）。操作（「実行」など）は吹き出しの中の操作なので secondary の sm を渡す。
 */

/** 文言（3 製品で同じ。既定は {@link sqlBlockLabels}）。 */
export interface SqlBlockLabels {
  copy: string;
  copied: string;
  copyFailed: string;
  /** 安全検査を通り、まだ実行していない。 */
  safe: string;
  /** 安全検査を通り、実行した。 */
  safeExecuted: string;
  unchecked: string;
  uncheckedExecuted: string;
  /** 安全検査で止めた。 */
  blocked: string;
}

export function sqlBlockLabels(overrides: Partial<SqlBlockLabels> = {}): SqlBlockLabels {
  return {
    copy: "SQL をコピー",
    copied: "SQL をコピーしました。",
    copyFailed: "SQL をコピーできませんでした。",
    safe: "安全検査済み・未実行",
    safeExecuted: "安全検査済み・実行済み",
    unchecked: "安全確認を省略・未実行",
    uncheckedExecuted: "安全確認を省略・実行済み",
    blocked: "安全検査でブロック",
    ...overrides,
  };
}

export const DEFAULT_SQL_BLOCK_LABELS: SqlBlockLabels = sqlBlockLabels();

/** SQL の安全検査と実行の状態のバッジ（色だけでなく文言とアイコンで分ける）。 */
export function SqlSafetyBadge({
  safe,
  executed,
  checked = true,
  labels = DEFAULT_SQL_BLOCK_LABELS,
}: {
  safe: boolean;
  executed: boolean;
  checked?: boolean;
  labels?: SqlBlockLabels;
}) {
  return (
    <StatusBadge
      variant={!safe ? "danger" : checked ? "success" : "neutral"}
      label={!safe ? labels.blocked : !checked ? (executed ? labels.uncheckedExecuted : labels.unchecked) : executed ? labels.safeExecuted : labels.safe}
    />
  );
}

export interface ChatSqlBlockProps {
  sql: string;
  /** 左の状態のバッジ（`SqlSafetyBadge` など）。 */
  badges: ReactNode;
  /** コピーの右の操作。 */
  actions?: ReactNode;
  labels?: SqlBlockLabels;
  onCopied?: () => void;
  onCopyError?: (message: string) => void;
  testId?: string;
}

export function ChatSqlBlock({
  sql,
  badges,
  actions,
  labels = DEFAULT_SQL_BLOCK_LABELS,
  onCopied,
  onCopyError,
  testId,
}: ChatSqlBlockProps) {
  return (
    <>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">{badges}</div>
        <div className="flex flex-wrap items-center gap-2">
          <CopyButton
            size="sm"
            text={sql}
            label={labels.copy}
            copiedMessage={labels.copied}
            failedMessage={labels.copyFailed}
            onCopied={onCopied}
            onCopyError={onCopyError}
          />
          {actions}
        </div>
      </div>
      <pre
        className="max-w-full overflow-x-auto rounded-md border border-border bg-canvas p-3 text-sm [scrollbar-gutter:stable]"
        data-testid={testId}
      >
        <code>{sql}</code>
      </pre>
    </>
  );
}
