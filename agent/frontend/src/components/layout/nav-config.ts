import {
  BadgeCheck,
  Blocks,
  Bot,
  ChartColumn,
  ClipboardList,
  Cpu,
  FlaskConical,
  History,
  LockKeyhole,
  MessagesSquare,
  PlugZap,
  TableProperties,
  ThumbsUp,
  type LucideIcon,
} from "lucide-react";

import {
  ANSWER_NOTICE_NAV_ITEM,
  API_KEY_ADMIN_NAV_ITEM,
  TRACE_RECORDING_SETTINGS_NAV_ITEM,
  CHAT_ATTACHMENT_SETTINGS_NAV_ITEM,
  SYSTEM_SETTINGS_NAV_ITEMS,
  USER_ROLE_NAV_ITEMS,
  type HasPermission,
  type SystemSettingsKey,
  type UserRoleKey,
} from "@production-ready/system-settings";

import { APP_ROUTES } from "@/lib/routes";
import type { I18nKey } from "@/lib/i18n";
import { MENU_PERMISSIONS } from "@/lib/permissions";

export interface NavItem {
  href: string;
  labelKey: I18nKey;
  sidebarLabelKey?: I18nKey;
  icon: LucideIcon;
  /** 画面を開くのに必要なメニュー権限（ナビの表示とルートの保護に使う。#215）。 */
  permission: string;
}

export interface NavSection {
  titleKey: I18nKey;
  items: NavItem[];
  collapsible?: boolean;
  /**
   * 利用者の開閉の保存値が無いとき、このセクションを閉じて出すか（共有 Sidebar の NavSection.defaultCollapsed）。
   * 3 製品で業務の入口の「AI 活用」だけを展開し、ほかのセクションは true にする（#1563）。
   */
  defaultCollapsed?: boolean;
}

/** 共通5項目（共有パッケージ）に付ける Agent の menu 権限（NL2SQL / RAG と同じコード）。 */
const SYSTEM_SETTINGS_MENU_PERMISSIONS = {
  oci: MENU_PERMISSIONS.settingsOci,
  uploadStorage: MENU_PERMISSIONS.settingsUploadStorage,
  model: MENU_PERMISSIONS.settingsModel,
  database: MENU_PERMISSIONS.settingsDatabase,
  appearance: MENU_PERMISSIONS.settingsAppearance,
} satisfies Record<SystemSettingsKey, string>;

/** 共通2項目（ユーザー管理・ロール管理）に付ける Agent の menu 権限。 */
const USER_ROLE_MENU_PERMISSIONS = {
  users: MENU_PERMISSIONS.securityUsers,
  roles: MENU_PERMISSIONS.securityRoles,
} satisfies Record<UserRoleKey, string>;

/**
 * Agent コンソールのサイドナビ構成（共有 Sidebar が消費する）。並び方は NL2SQL / RAG と同じで、上に一般の
 * 利用者が使う画面、下に管理者の画面を置く（AI 活用 → Agent 構築 → 改善・運用 → セキュリティ設定 →
 * 共通のユーザーとロール → 運用設定 → 共通のシステム設定。#658 / #774 / #776 / #791）。
 */
export const NAV_SECTIONS: NavSection[] = [
  {
    // 一般の利用者が毎日使う画面（#791）。NL2SQL の「AI 活用」と同じ名前・位置。先頭のチャットが既定の入口
    // （RAG と同じ。route-permissions の defaultEntryRoute）。
    titleKey: "nav.section.use",
    items: [
      // 業務利用者の入口（#768。RAG のチャットと同じアイコン）。
      { href: APP_ROUTES.chat, labelKey: "nav.chat", icon: MessagesSquare, permission: MENU_PERMISSIONS.chat },
      { href: APP_ROUTES.approvals, labelKey: "nav.approvals", icon: BadgeCheck, permission: MENU_PERMISSIONS.approvals },
      // Run の一覧（URL・権限コードは変えない）。名前とアイコンは NL2SQL の「実行履歴」と同じ（#791）。
      { href: APP_ROUTES.runs, labelKey: "nav.runs", icon: History, permission: MENU_PERMISSIONS.runs },
    ],
  },
  {
    // 管理者が業務 Agent を作る画面（#791）。RAG の「ナレッジ構築」・NL2SQL の「データ準備」に当たる。
    titleKey: "nav.section.build",
    defaultCollapsed: true,

    items: [
      { href: APP_ROUTES.agents, labelKey: "nav.agents", icon: Bot, permission: MENU_PERMISSIONS.agents },
      { href: APP_ROUTES.skills, labelKey: "nav.skills", icon: Blocks, permission: MENU_PERMISSIONS.skills },
    ],
  },
  {
    // 業務 Agent の回答と品質を確かめて直す画面。RAG / NL2SQL の「改善・運用」（nav.section.improve）と
    // 同じ名前・同じ位置・同じ並び・同じアイコン（品質評価は FlaskConical、フィードバックは ThumbsUp）にする
    // （#658 / #772 / #774 / #776）。
    titleKey: "nav.section.improve",
    defaultCollapsed: true,

    items: [
      {
        href: APP_ROUTES.evaluation,
        labelKey: "nav.evaluation",
        icon: FlaskConical,
        permission: MENU_PERMISSIONS.evaluation,
      },
      { href: APP_ROUTES.feedback, labelKey: "nav.feedback", icon: ThumbsUp, permission: MENU_PERMISSIONS.feedback },
      // 利用状況（#772）。RAG / NL2SQL に無い項目なので、共通の 2 項目の後ろに置く。
      { href: APP_ROUTES.usage, labelKey: "nav.usage", icon: ChartColumn, permission: MENU_PERMISSIONS.usage },
      // 監査ログ（実行をまたぐツールの実行・承認・警告の記録）は、実行を見守る運用の画面なので利用状況の後ろ（#791）。
      { href: APP_ROUTES.audit, labelKey: "nav.audit", icon: ClipboardList, permission: MENU_PERMISSIONS.audit },
    ],
  },
  {
    // Agent 固有のセキュリティ（ロールへの機能権限とエージェント / 検索・回答プロファイルの対象範囲の付与。#215）。
    titleKey: "nav.section.security",
    defaultCollapsed: true,

    items: [
      {
        href: APP_ROUTES.securityPermissions,
        labelKey: "nav.securityPermissions",
        icon: LockKeyhole,
        permission: MENU_PERMISSIONS.securityPermissions,
      },
    ],
  },
  {
    // 3製品で共通のユーザー管理・ロール管理（画面は platform の共有パッケージ。#206）。
    titleKey: "nav.section.userRoles",
    defaultCollapsed: true,

    // 共有パッケージの型は platform 側の @types/react で解決されるため、icon だけ Agent の型へそろえる
    // （実体は同じ lucide-react のコンポーネント）。
    items: [
      ...USER_ROLE_NAV_ITEMS.map((item) => ({
        ...item,
        icon: item.icon as LucideIcon,
        permission: USER_ROLE_MENU_PERMISSIONS[item.key],
      })),
      {
        href: API_KEY_ADMIN_NAV_ITEM.href,
        labelKey: "nav.securityApiKeys",
        icon: API_KEY_ADMIN_NAV_ITEM.icon as LucideIcon,
        permission: MENU_PERMISSIONS.securityApiKeys,
      },
    ],
  },
  {
    // Agent 固有の運用設定（実行環境・接続先。#87 / #791）。先頭はシステムテーブル
    // （RAG / NL2SQL と同じ並びとアイコン。#658 / #751）。
    titleKey: "nav.section.operations",
    defaultCollapsed: true,

    items: [
      {
        href: APP_ROUTES.settingsSystemTables,
        labelKey: "nav.settingsSystemTables",
        sidebarLabelKey: "nav.settingsSystemTables.sidebar",
        icon: TableProperties,
        permission: MENU_PERMISSIONS.settingsSystemTables,
      },
      // チャットの添付（設定は製品ごと。3 製品で同じ位置。回答の注意書きの上。#2517・#2546）。
      {
        href: TRACE_RECORDING_SETTINGS_NAV_ITEM.href,
        labelKey: "nav.settingsTraceRecording",
        icon: TRACE_RECORDING_SETTINGS_NAV_ITEM.icon as LucideIcon,
        permission: MENU_PERMISSIONS.settingsTraceRecording,
      },
      {
        href: CHAT_ATTACHMENT_SETTINGS_NAV_ITEM.href,
        labelKey: "nav.settingsChatAttachments",
        icon: CHAT_ATTACHMENT_SETTINGS_NAV_ITEM.icon as LucideIcon,
        permission: MENU_PERMISSIONS.settingsChatAttachments,
      },
      // 回答の注意書き（設定は製品ごと。3 製品で同じ位置。#2449）。
      {
        href: ANSWER_NOTICE_NAV_ITEM.href,
        labelKey: "nav.settingsAnswerNotice",
        icon: ANSWER_NOTICE_NAV_ITEM.icon as LucideIcon,
        permission: MENU_PERMISSIONS.settingsAnswerNotice,
      },
      // 組み込み Runtime の状態とモデルの確認（RAG の「サービス」と同じく運用の画面。#791）。
      { href: APP_ROUTES.runtimes, labelKey: "nav.runtimes", icon: Cpu, permission: MENU_PERMISSIONS.runtimes },
      {
        href: APP_ROUTES.settingsMcpConnections,
        labelKey: "nav.settingsMcpConnections",
        icon: PlugZap,
        permission: MENU_PERMISSIONS.settingsExternalMcp,
      },
    ],
  },
  {
    // 3製品で共通のシステム設定（画面は platform の共有パッケージ。#70）。
    titleKey: "nav.section.settings",
    defaultCollapsed: true,

    items: SYSTEM_SETTINGS_NAV_ITEMS.map((item) => ({
      ...item,
      icon: item.icon as LucideIcon,
      permission: SYSTEM_SETTINGS_MENU_PERMISSIONS[item.key],
    })),
  },
];

/** ナビの全項目（並び順どおり）。ルートの権限と既定の入口の判定に使う。 */
export const NAV_ITEMS: readonly NavItem[] = NAV_SECTIONS.flatMap((section) => section.items);

/** 利用者に権限のある項目だけを残したナビ構成。項目が 0 件になったセクションは除く（#215）。 */
export function visibleNavSections(
  hasPermission: HasPermission,
  sections: readonly NavSection[] = NAV_SECTIONS,
): NavSection[] {
  return sections
    .map((section) => ({
      ...section,
      items: section.items.filter((item) => hasPermission(item.permission)),
    }))
    .filter((section) => section.items.length > 0);
}
