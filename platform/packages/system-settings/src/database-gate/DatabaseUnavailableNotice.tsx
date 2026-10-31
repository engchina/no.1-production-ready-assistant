import { useMemo, type ComponentProps, type ReactNode } from "react";
import {
  ArrowRight,
  DatabaseZap,
  Hourglass,
  PowerOff,
  RefreshCw,
  ServerCrash,
  ServerOff,
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
  StatusBadge,
  type ButtonLinkComponent,
  type FeedbackTone,
  type StatusVariant,
} from "@production-ready/ui";

import { formatMessage } from "../auth/messages";
import { DATABASE_MESSAGES, type DatabaseMessageKey } from "../database/messages";
import {
  DATABASE_GATE_MESSAGES,
  databaseCheckMessageKey,
  type DatabaseGateMessageKey,
  type DatabaseGateMessages,
} from "./messages";
import { ADB_MANAGEMENT_ANCHOR_ID, type DatabaseGateRoutes, type DatabaseNoticeStatus } from "./types";

/** 見出しの id（カードの `aria-labelledby`。3 製品の E2E が参照する）。 */
export const DATABASE_UNAVAILABLE_TITLE_ID = "database-unavailable-title";

/**
 * 接続できない（`unreachable`）ときの ADB の状態の分け方（#820）。
 * `unknown` は ADB OCID が未設定か、状態を取得できなかったとき。
 */
export type AdbLifecycleGroup = "stopped" | "starting" | "unavailable" | "available" | "unknown";

export function adbLifecycleGroup(state: string | null | undefined): AdbLifecycleGroup {
  const value = state?.trim().toUpperCase();
  if (!value) return "unknown";
  if (value === "AVAILABLE") return "available";
  if (value === "STARTING") return "starting";
  if (value === "STOPPED" || value === "STOPPING") return "stopped";
  return "unavailable";
}

/** 案内の種類（未設定・接続できない（ADB の状態で細分）・初期化が必要・状態を確認できない）。 */
export type DatabaseNoticeKind =
  | DatabaseNoticeStatus
  | "adb_stopped"
  | "adb_starting"
  | "adb_unavailable"
  | "adb_available";

/** 設定の画面への導線。 */
export interface DatabaseNoticeAction {
  href: string;
  label: DatabaseGateMessageKey;
  icon: LucideIcon;
  target: "database" | "systemTables";
}

/** 案内の見出し・本文・アイコン・導線（状態 × ADB の状態 × 権限）。 */
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
  adbLifecycleState?: string | null;
  routes: DatabaseGateRoutes;
  canManageDatabase: boolean;
  canManageSystemTables: boolean;
}

/** データベース設定の URL（ADB 管理のカードへ案内するときだけ hash を付ける）。 */
export function databaseSettingsHref(path: string, adbCard: boolean): string {
  const base = path.split("#", 1)[0];
  return adbCard ? `${base}#${ADB_MANAGEMENT_ANCHOR_ID}` : base;
}

interface UnreachableCopy {
  kind: DatabaseNoticeKind;
  title: DatabaseGateMessageKey;
  message: DatabaseGateMessageKey;
  contactAdmin: DatabaseGateMessageKey;
  icon: LucideIcon;
  tone: FeedbackTone;
  /** データベース設定の ADB 管理のカードへ案内するか。 */
  adbCard: boolean;
}

const UNREACHABLE_COPY: Record<AdbLifecycleGroup, UnreachableCopy> = {
  unknown: {
    kind: "unreachable",
    title: "dbGate.unreachable.title",
    message: "dbGate.unreachable.message",
    contactAdmin: "dbGate.unreachable.contactAdmin",
    icon: Unplug,
    tone: "warning",
    // ADB の状態が分からないときも、多い原因（停止中）の復旧の場所（ADB 管理のカード）へ案内する。
    adbCard: true,
  },
  stopped: {
    kind: "adb_stopped",
    title: "dbGate.adbStopped.title",
    message: "dbGate.adbStopped.message",
    contactAdmin: "dbGate.adbStopped.contactAdmin",
    icon: PowerOff,
    tone: "warning",
    adbCard: true,
  },
  starting: {
    kind: "adb_starting",
    title: "dbGate.adbStarting.title",
    message: "dbGate.adbStarting.message",
    contactAdmin: "dbGate.adbStarting.contactAdmin",
    icon: Hourglass,
    tone: "info",
    adbCard: true,
  },
  unavailable: {
    kind: "adb_unavailable",
    title: "dbGate.adbUnavailable.title",
    message: "dbGate.adbUnavailable.message",
    contactAdmin: "dbGate.adbUnavailable.contactAdmin",
    icon: ServerOff,
    tone: "warning",
    adbCard: true,
  },
  available: {
    kind: "adb_available",
    title: "dbGate.unreachable.title",
    message: "dbGate.adbAvailable.message",
    contactAdmin: "dbGate.adbAvailable.contactAdmin",
    icon: Unplug,
    tone: "warning",
    adbCard: false,
  },
};

/**
 * 状態ごとに見出し・本文・アイコン・導線を分ける（#820。単体テストのため純粋関数）。
 * - 未設定 → データベース設定（接続情報）
 * - 接続できない → データベース設定の ADB 管理のカード（ADB は起動済みと分かっているときは接続情報）
 * - 初期化が必要 → システムテーブル（`systemTables` が無い製品はデータベース設定）
 * - 状態を確認できない → 導線なし（再試行だけ）
 * - その画面を開けない利用者 → 導線を出さず、システム管理者への連絡を案内する
 */
export function databaseNoticeContent({
  status,
  adbLifecycleState,
  routes,
  canManageDatabase,
  canManageSystemTables,
}: DatabaseNoticeContentInput): DatabaseNoticeContent {
  const databaseAction = (adbCard: boolean): DatabaseNoticeAction => ({
    href: databaseSettingsHref(routes.databaseSettings, adbCard),
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
      action: canManageDatabase ? databaseAction(false) : null,
      footer: canManageDatabase ? "dbGate.settingsHint" : "dbGate.contactAdmin.footer",
    };
  }
  if (status === "unreachable") {
    const copy = UNREACHABLE_COPY[adbLifecycleGroup(adbLifecycleState)];
    return {
      kind: copy.kind,
      title: copy.title,
      message: canManageDatabase ? copy.message : copy.contactAdmin,
      icon: copy.icon,
      tone: copy.tone,
      action: canManageDatabase ? databaseAction(copy.adbCard) : null,
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
        : databaseAction(false);
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

const ADB_LIFECYCLE_VARIANT: Record<AdbLifecycleGroup, StatusVariant> = {
  stopped: "neutral",
  starting: "info",
  unavailable: "warning",
  available: "success",
  unknown: "neutral",
};

/** ADB の状態の表示名（データベース設定の画面と同じ文言。無い値はそのまま出す）。 */
export function adbLifecycleLabel(state: string): string {
  const key = `settings.adb.lifecycle.${state.trim().toUpperCase()}`;
  return Object.prototype.hasOwnProperty.call(DATABASE_MESSAGES, key)
    ? DATABASE_MESSAGES[key as DatabaseMessageKey]
    : state;
}

export interface DatabaseUnavailableNoticeProps {
  /** `gate`＝主領域の中央の案内カード、`banner`＝カードやページの先頭の常設の Banner。 */
  mode?: "gate" | "banner";
  status?: DatabaseNoticeStatus;
  /** 診断コード（状態 API の `check` など）。`ok` と空は出さない。 */
  reasonCode?: string | null;
  /** ADB のライフサイクル状態（状態 API の `adb_lifecycle_state`。`unreachable` のときだけ使う）。 */
  adbLifecycleState?: string | null;
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
   * banner にデータベース設定（ADB 管理のカード）へのリンクを添える（`canManageDatabase` のときだけ
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
  adbLifecycleState,
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
    adbLifecycleState,
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
                  to={databaseSettingsHref(routes.databaseSettings, true)}
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
  const adbState = status === "unreachable" ? adbLifecycleState?.trim() : undefined;
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
        adbState || code ? (
          <>
            {adbState ? (
              <div className="flex justify-center pb-1" data-testid="database-gate-adb-state">
                <StatusBadge
                  variant={ADB_LIFECYCLE_VARIANT[adbLifecycleGroup(adbState)]}
                  label={formatMessage(m["dbGate.adbState"], { state: adbLifecycleLabel(adbState) })}
                />
              </div>
            ) : null}
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
