import { useMemo, type ComponentProps, type ReactNode } from "react";
import {
  ArrowRight,
  DatabaseZap,
  RefreshCw,
  ServerCrash,
  Settings,
  Unplug,
  Wrench,
  type LucideIcon,
} from "lucide-react";
import { Link } from "react-router-dom";
import {
  Banner,
  BlockedPageNotice,
  Button,
  ButtonLink,
  type ButtonLinkComponent,
  type FeedbackTone,
} from "@production-ready/ui";

import { formatMessage } from "../auth/messages";
import {
  DATABASE_GATE_MESSAGES,
  databaseCheckMessageKey,
  type DatabaseGateMessageKey,
  type DatabaseGateMessages,
} from "./messages";
import type { DatabaseGateRoutes, DatabaseNoticeStatus } from "./types";

/** 見出しの id（カードの `aria-labelledby`。3 製品の E2E が参照する）。 */
export const DATABASE_UNAVAILABLE_TITLE_ID = "database-unavailable-title";

/** 案内の種類（未設定・接続できない・初期化が必要・状態を確認できない）。 */
export type DatabaseNoticeKind = DatabaseNoticeStatus;

/** 設定の画面への導線。 */
export interface DatabaseNoticeAction {
  href: string;
  label: DatabaseGateMessageKey;
  icon: LucideIcon;
  target: "database" | "systemTables";
}

/** 案内の見出し・本文・アイコン・導線（状態 × 権限）。 */
export interface DatabaseNoticeContent {
  kind: DatabaseNoticeKind;
  title: DatabaseGateMessageKey;
  message: DatabaseGateMessageKey;
  icon: LucideIcon;
  tone: FeedbackTone;
  /** 設定の画面への導線（その画面を開けない利用者・状態を確認できないときは null）。 */
  action: DatabaseNoticeAction | null;
  footer: DatabaseGateMessageKey;
}

export interface DatabaseNoticeContentInput {
  status: DatabaseNoticeStatus;
  routes: DatabaseGateRoutes;
  canManageDatabase: boolean;
  canManageSystemTables: boolean;
}

/** データベース設定の URL（hash は付けない。接続設定と Wallet は同じカードにある）。 */
export function databaseSettingsHref(path: string): string {
  return path.split("#", 1)[0];
}

/**
 * 状態ごとに見出し・本文・アイコン・導線を分ける（#820。単体テストのため純粋関数）。
 * - 未設定 → データベース設定（接続情報）
 * - 接続できない → データベース設定（接続設定と Wallet を確かめる。DB の起動・停止は画面では扱わない。#10）
 * - 初期化が必要 → システムテーブル（`systemTables` が無い製品はデータベース設定）
 * - 状態を確認できない → 導線なし（再試行だけ）
 * - その画面を開けない利用者 → 導線を出さず、システム管理者への連絡を案内する
 */
export function databaseNoticeContent({
  status,
  routes,
  canManageDatabase,
  canManageSystemTables,
}: DatabaseNoticeContentInput): DatabaseNoticeContent {
  const databaseAction = (): DatabaseNoticeAction => ({
    href: databaseSettingsHref(routes.databaseSettings),
    label: "dbGate.openDatabaseSettings",
    icon: Settings,
    target: "database",
  });

  if (status === "not_configured") {
    return {
      kind: status,
      title: "dbGate.notConfigured.title",
      message: canManageDatabase ? "dbGate.notConfigured.message" : "dbGate.notConfigured.contactAdmin",
      icon: Settings,
      tone: "warning",
      action: canManageDatabase ? databaseAction() : null,
      footer: canManageDatabase ? "dbGate.settingsHint" : "dbGate.contactAdmin.footer",
    };
  }
  if (status === "unreachable") {
    return {
      kind: status,
      title: "dbGate.unreachable.title",
      message: canManageDatabase ? "dbGate.unreachable.message" : "dbGate.unreachable.contactAdmin",
      icon: Unplug,
      tone: "warning",
      action: canManageDatabase ? databaseAction() : null,
      footer: canManageDatabase ? "dbGate.settingsHint" : "dbGate.contactAdmin.footer",
    };
  }
  if (status === "setup_required") {
    // システムテーブルの画面が無い製品は、データベース設定へ案内する。
    const canManage = routes.systemTables ? canManageSystemTables : canManageDatabase;
    let action: DatabaseNoticeAction | null = null;
    if (canManage) {
      action = routes.systemTables
        ? { href: routes.systemTables, label: "dbGate.openSystemTables", icon: DatabaseZap, target: "systemTables" }
        : databaseAction();
    }
    return {
      kind: status,
      title: "dbGate.setupRequired.title",
      message: canManage ? "dbGate.setupRequired.message" : "dbGate.setupRequired.contactAdmin",
      icon: Wrench,
      tone: "warning",
      action,
      footer: canManage ? "dbGate.setupRequired.settingsHint" : "dbGate.contactAdmin.footer",
    };
  }
  const admin = canManageDatabase || canManageSystemTables;
  return {
    kind: "check_failed",
    title: "dbGate.checkFailed.title",
    message: admin ? "dbGate.checkFailed.message" : "dbGate.checkFailed.contactAdmin",
    icon: ServerCrash,
    tone: "warning",
    action: null,
    footer: admin ? "dbGate.settingsHint" : "dbGate.contactAdmin.footer",
  };
}

export interface DatabaseUnavailableNoticeProps {
  /** `gate`＝主領域の中央の案内カード、`banner`＝カードやページの先頭の常設の Banner。 */
  mode?: "gate" | "banner";
  status?: DatabaseNoticeStatus;
  /** 診断コード（状態 API の `check` など）。`ok` と空は出さない。 */
  reasonCode?: string | null;
  routes: DatabaseGateRoutes;
  messages?: Partial<DatabaseGateMessages>;
  /**
   * データベース設定を開けるか（システム設定のデータベースの権限）。false の利用者には設定への
   * 導線を出さず、システム管理者への連絡を案内する。既定は false（分からなければ導線を出さない。#820）。
   */
  canManageDatabase?: boolean;
  /** システムテーブルの画面を開けるか（`setup_required` の導線）。既定は false（#820）。 */
  canManageSystemTables?: boolean;
  /** 設定を開くリンクの `state.returnTo`（設定画面から元の画面へ戻るため）。 */
  returnTo?: string;
  /** 省略すると再試行を出さない（banner だけ。gate は常に再試行を出す）。 */
  onRetry?: () => void;
  isRetrying?: boolean;
  /** 見出しの上書き（製品固有の状態の案内・縮退の banner）。 */
  title?: string;
  /** 本文の上書き（banner では本文の中身）。 */
  message?: ReactNode;
  /**
   * banner にデータベース設定へのリンクを添える（`canManageDatabase` のときだけ
   * 出す）。
   */
  settingsLink?: boolean;
  className?: string;
}

/** 画面に出す診断コード（`ok` と空は出さない）。 */
export function databaseReasonCode(code: string | null | undefined): string | null {
  const value = code?.trim();
  return value && value !== "ok" ? value : null;
}

/** 設定の画面へ移る `Link`（戻り先を `state.returnTo` に入れる）。 */
function useReturnToLink(returnTo: string | undefined): ButtonLinkComponent {
  return useMemo(() => {
    function ReturnToLink(props: ComponentProps<ButtonLinkComponent>) {
      return <Link {...props} state={returnTo ? { returnTo } : undefined} />;
    }
    return ReturnToLink;
  }, [returnTo]);
}

/**
 * DB が使えないときの案内（#325。NL2SQL の見た目が基準）。
 * 状態（未設定・接続できない・初期化が必要・状態を確認できない）ごとに見出し・本文・アイコン・導線を分け、
 * 設定の画面を開けない利用者にはシステム管理者への連絡を案内する（#820）。再試行は全状態で出す。
 */
export function DatabaseUnavailableNotice({
  mode = "gate",
  status = "unreachable",
  reasonCode,
  routes,
  messages,
  canManageDatabase = false,
  canManageSystemTables = false,
  returnTo,
  onRetry,
  isRetrying = false,
  title,
  message,
  settingsLink = false,
  className,
}: DatabaseUnavailableNoticeProps) {
  const m: DatabaseGateMessages = { ...DATABASE_GATE_MESSAGES, ...messages };
  const linkComponent = useReturnToLink(returnTo);
  const content = databaseNoticeContent({
    status,
    routes,
    canManageDatabase,
    canManageSystemTables,
  });
  const retryButton = onRetry ? (
    <Button
      type="button"
      size={mode === "banner" ? "sm" : "md"}
      variant="secondary"
      onClick={onRetry}
      loading={isRetrying}
      icon={RefreshCw}
      data-testid="database-gate-retry"
    >
      {m["common.retry"]}
    </Button>
  ) : null;

  if (mode === "banner") {
    const showSettingsLink = settingsLink && canManageDatabase;
    return (
      <Banner
        severity="warning"
        title={title ?? m[content.title]}
        className={className}
        action={
          retryButton || showSettingsLink ? (
            <>
              {retryButton}
              {showSettingsLink ? (
                <ButtonLink
                  to={databaseSettingsHref(routes.databaseSettings)}
                  linkComponent={linkComponent}
                  variant="secondary"
                  size="sm"
                  icon={Settings}
                  trailingIcon={ArrowRight}
                >
                  {m["dbGate.openDatabaseSettings"]}
                </ButtonLink>
              ) : null}
            </>
          ) : undefined
        }
      >
        {message}
      </Banner>
    );
  }

  const code = databaseReasonCode(reasonCode);
  const codeMessageKey = code ? databaseCheckMessageKey(code) : undefined;
  const action = content.action;

  return (
    <BlockedPageNotice
      title={title ?? m[content.title]}
      titleId={DATABASE_UNAVAILABLE_TITLE_ID}
      icon={content.icon}
      tone={content.tone}
      message={message ?? m[content.message]}
      className={className}
      testId="database-unavailable-notice"
      details={
        code ? (
          <>
            {codeMessageKey ? <p>{m[codeMessageKey]}</p> : null}
            {code ? <p role="status">{formatMessage(m["dbGate.reasonCode"], { code })}</p> : null}
          </>
        ) : undefined
      }
      actions={
        <>
          {action ? (
            <ButtonLink
              to={action.href}
              linkComponent={linkComponent}
              variant="primary"
              size="md"
              icon={action.icon}
              trailingIcon={ArrowRight}
              testId={
                action.target === "systemTables"
                  ? "database-gate-open-system-tables"
                  : "database-gate-open-database-settings"
              }
            >
              {m[action.label]}
            </ButtonLink>
          ) : null}
          {retryButton}
        </>
      }
      footer={m[content.footer]}
    />
  );
}
