import { useEffect, type ComponentType, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useLocation } from "react-router-dom";
import { PageLoadingState } from "@production-ready/ui";

import { SYSTEM_SETTINGS_PATHS } from "../paths";
import { DatabaseUnavailableNotice } from "./DatabaseUnavailableNotice";
import { DATABASE_GATE_MESSAGES, type DatabaseGateMessages } from "./messages";
import {
  DATABASE_STATUS_QUERY_KEY,
  DATABASE_UNAVAILABLE_EVENT,
  type DatabaseAvailability,
  type DatabaseGateRoutes,
  type DatabaseNoticeStatus,
  type DatabaseStatusApi,
  type DatabaseStatusData,
  type DatabaseUnavailableEventDetail,
} from "./types";
import { useDatabaseStatus, type DatabaseContextChangeHandler } from "./useDatabaseStatus";

/** 読み込み中の表示の testId（3 製品の E2E が参照する）。 */
export const DATABASE_GATE_LOADING_TEST_ID = "database-gate-loading";

/**
 * ゲートを通さない画面（#325。3 製品で同じ）。システム設定の 5 画面（OCI 認証・アップロード保存先・
 * モデル・データベース）と、その入口の `/settings` だけ。DB の復旧に使う画面なので塞がない。
 * ユーザー管理・ロール管理・製品固有の設定は DB にデータを持つため、ゲートを通す（#214）。
 */
export function isDatabaseGateExemptPath(pathname: string): boolean {
  if (pathname === "/settings") return true;
  return Object.values(SYSTEM_SETTINGS_PATHS).some((route) => matchesPath(pathname, route));
}

function matchesPath(pathname: string, route: string): boolean {
  const path = route.split(/[?#]/, 1)[0];
  return pathname === path || pathname.startsWith(`${path}/`);
}

export function databaseNoticeStatus(status: DatabaseAvailability | undefined): DatabaseNoticeStatus {
  return status === "not_configured" || status === "setup_required" || status === "unreachable"
    ? status
    : "check_failed";
}

/** ゲートが何を描くか（状態ごとの分岐。単体テストのため純粋関数にしている）。 */
export type DatabaseGateView =
  | { kind: "children" }
  | { kind: "checking" }
  | {
      kind: "notice";
      status: DatabaseNoticeStatus;
      reasonCode: string | null;
    }
  | { kind: "secondary" };

export function databaseGateView({
  exempt,
  onSystemTables,
  isPending,
  isError,
  data,
  hasSecondaryGate,
}: {
  exempt: boolean;
  /** システムテーブルの管理の画面か（`setup_required` でも開ける）。 */
  onSystemTables: boolean;
  isPending: boolean;
  isError: boolean;
  data: DatabaseStatusData | undefined;
  hasSecondaryGate: boolean;
}): DatabaseGateView {
  if (exempt) return { kind: "children" };
  if (isPending) return { kind: "checking" };
  if (isError) return { kind: "notice", status: "check_failed", reasonCode: null };
  const status = data?.status;
  const allowed = status === "ok" || (onSystemTables && status === "setup_required");
  if (!allowed) {
    return { kind: "notice", status: databaseNoticeStatus(status), reasonCode: data?.check ?? null };
  }
  // システムテーブルの管理は製品の追加の確認（保存領域など）より前に開ける。
  if (onSystemTables || !hasSecondaryGate) return { kind: "children" };
  return { kind: "secondary" };
}

export interface DatabaseGateNoticeOptions {
  status?: DatabaseNoticeStatus;
  reasonCode?: string | null;
  onRetry: () => void;
  isRetrying?: boolean;
  title?: string;
  message?: string;
}

/** DB が使えるようになった後の、製品固有の確認（NL2SQL の保存領域など）。 */
export interface DatabaseSecondaryGateProps {
  children: ReactNode;
  returnTo: string;
  /** 共通の案内（見た目・導線・文言をゲートとそろえる）。 */
  renderNotice: (options: DatabaseGateNoticeOptions) => ReactNode;
  /** 共通の読み込み中の表示。 */
  renderChecking: (label: string, operationKey?: string) => ReactNode;
}

export interface DatabaseGateProps {
  api: DatabaseStatusApi;
  routes: DatabaseGateRoutes;
  /** ゲートを通さない画面の判定（既定は `isDatabaseGateExemptPath`）。 */
  isExempt?: (pathname: string) => boolean;
  /** 製品の i18n の値で既定の文言を上書きする（製品名が入る文言など）。 */
  messages?: Partial<DatabaseGateMessages>;
  /**
   * 利用者がデータベース設定を開けるか（製品の権限から渡す）。false なら設定への導線を出さず、
   * システム管理者への連絡を案内する。既定は false（分からなければ導線を出さない。#820）。
   */
  canManageDatabase?: boolean;
  /** 利用者がシステムテーブルの画面を開けるか（`setup_required` の導線）。既定は false（#820）。 */
  canManageSystemTables?: boolean;
  onContextChange?: DatabaseContextChangeHandler;
  /** 再試行の直前に呼ぶ（NL2SQL は遅れて届いた古い確認の結果を捨てる）。 */
  onBeforeRetry?: () => void;
  secondaryGate?: ComponentType<DatabaseSecondaryGateProps>;
  children: ReactNode;
}

function isDatabaseStatusData(value: unknown): value is DatabaseStatusData {
  const status = (value as { status?: unknown } | null)?.status;
  return status === "ok" || status === "not_configured" || status === "unreachable" || status === "setup_required";
}

/**
 * DB ゲート（#325。3 製品共通。見た目と振る舞いは NL2SQL が基準）。
 * ゲートを通さない画面以外は、DB が使えるまで業務画面を描かず、原因と復旧の導線を出す。
 */
export function DatabaseGate({
  api,
  routes,
  isExempt = isDatabaseGateExemptPath,
  messages,
  canManageDatabase = false,
  canManageSystemTables = false,
  onContextChange,
  onBeforeRetry,
  secondaryGate: SecondaryGate,
  children,
}: DatabaseGateProps) {
  const location = useLocation();
  const queryClient = useQueryClient();
  const exempt = isExempt(location.pathname);
  const onSystemTables = routes.systemTables ? matchesPath(location.pathname, routes.systemTables) : false;
  const database = useDatabaseStatus(api, { enabled: !exempt, onContextChange });

  // 業務 API の失敗から DB の不通を確かめた通知で、今の状態を置き換える（案内がすぐ出る）。
  useEffect(() => {
    const handle = (event: Event) => {
      const detail = (event as CustomEvent<DatabaseUnavailableEventDetail>).detail;
      if (detail?.kind === "database" && isDatabaseStatusData(detail.database)) {
        queryClient.setQueryData(DATABASE_STATUS_QUERY_KEY, detail.database);
      }
    };
    window.addEventListener(DATABASE_UNAVAILABLE_EVENT, handle);
    return () => window.removeEventListener(DATABASE_UNAVAILABLE_EVENT, handle);
  }, [queryClient]);

  const view = databaseGateView({
    exempt,
    onSystemTables,
    isPending: database.isPending,
    isError: database.isError,
    data: database.data,
    hasSecondaryGate: Boolean(SecondaryGate),
  });
  if (view.kind === "children") return <>{children}</>;

  const m: DatabaseGateMessages = { ...DATABASE_GATE_MESSAGES, ...messages };
  const returnTo = `${location.pathname}${location.search}${location.hash}`;
  const renderChecking = (label: string, operationKey = "dbGate.checking") => (
    <DatabaseGateChecking label={label} operationKey={operationKey} />
  );
  const renderNotice = (options: DatabaseGateNoticeOptions) => (
    <DatabaseUnavailableNotice
      routes={routes}
      messages={messages}
      returnTo={returnTo}
      canManageDatabase={canManageDatabase}
      canManageSystemTables={canManageSystemTables}
      {...options}
    />
  );

  if (view.kind === "checking") return renderChecking(m["dbGate.checking"]);
  if (view.kind === "notice") {
    return renderNotice({
      status: view.status,
      reasonCode: view.reasonCode,
      onRetry: () => {
        onBeforeRetry?.();
        void database.refetch();
      },
      isRetrying: database.isFetching,
    });
  }
  if (!SecondaryGate) return <>{children}</>;
  return (
    <SecondaryGate returnTo={returnTo} renderNotice={renderNotice} renderChecking={renderChecking}>
      {children}
    </SecondaryGate>
  );
}

/** 状態の確認中（ページ全体が使えない初期処理なので placement は page）。 */
export function DatabaseGateChecking({ label, operationKey }: { label: string; operationKey: string }) {
  return (
    // 画面の中央に浮かせず、本文の先頭に本文と同じ幅で出す（ほかの読み込み中と同じ。#2510）。
    <PageLoadingState label={label} operationKey={operationKey} testId={DATABASE_GATE_LOADING_TEST_ID} />
  );
}
