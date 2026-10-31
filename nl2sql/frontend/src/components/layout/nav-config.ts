import {
  BookA,
  BotMessageSquare,
  Boxes,
  BrainCircuit,
  BriefcaseBusiness,
  Eye,
  FileCode2,
  FileKey,
  FileSpreadsheet,
  FlaskConical,
  History,
  LockKeyhole,
  MessageSquareCode,
  MessageSquareText,
  MessagesSquare,
  Network,
  ScrollText,
  Shapes,
  ShieldCheck,
  Sparkles,
  SquareTerminal,
  Table2,
  TableProperties,
  Tags,
  ThumbsUp,
  type LucideIcon,
} from "lucide-react";

import { MENU_PERMISSIONS } from "@/features/security/menu-permissions";
import type { I18nKey } from "@/lib/i18n";
import {
  ANSWER_NOTICE_NAV_ITEM,
  TRACE_RECORDING_SETTINGS_NAV_ITEM,
  CHAT_ATTACHMENT_SETTINGS_NAV_ITEM,
  SYSTEM_SETTINGS_NAV_ITEMS,
  API_KEY_ADMIN_NAV_ITEM,
  USER_ROLE_NAV_ITEMS,
  type SystemSettingsKey,
  type UserRoleKey,
} from "@production-ready/system-settings";

import { APP_ROUTES } from "@/lib/routes";

export interface NavItem {
  href: string;
  labelKey: I18nKey;
  sidebarLabelKey?: I18nKey;
  icon: LucideIcon;
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

/** 共通5項目（共有パッケージ）に付ける NL2SQL の menu 権限。 */
const SYSTEM_SETTINGS_MENU_PERMISSIONS = {
  oci: MENU_PERMISSIONS.settingsOci,
  uploadStorage: MENU_PERMISSIONS.settingsUploadStorage,
  model: MENU_PERMISSIONS.settingsModel,
  database: MENU_PERMISSIONS.settingsDatabase,
  appearance: MENU_PERMISSIONS.settingsAppearance,
} satisfies Record<SystemSettingsKey, string>;

/** 共通2項目（ユーザー管理・ロール管理）に付ける NL2SQL の menu 権限（#206）。 */
const USER_ROLE_MENU_PERMISSIONS = {
  users: MENU_PERMISSIONS.securityUsers,
  roles: MENU_PERMISSIONS.securityRoles,
} satisfies Record<UserRoleKey, string>;

/**
 * NL2SQL コンソールのサイドナビ構成（共有 Sidebar が消費する）。下部の並びは 3 製品で同じ
 * （改善・運用 → セキュリティ設定 → 共通のユーザーとロール → 運用設定 → 共通のシステム設定。#658）。
 */
export const NAV_SECTIONS: NavSection[] = [
  {
    titleKey: "nav.section.use",
    items: [
      { href: APP_ROUTES.chat, labelKey: "nav.chat", icon: MessagesSquare, permission: MENU_PERMISSIONS.chat },
      { href: APP_ROUTES.query, labelKey: "nav.query", icon: Sparkles, permission: MENU_PERMISSIONS.query },
      {
        href: APP_ROUTES.directSql,
        labelKey: "nav.directSql",
        icon: FileCode2,
        permission: MENU_PERMISSIONS.directSql,
      },
      { href: APP_ROUTES.sqlToQuestion, labelKey: "nav.sqlToQuestion", icon: MessageSquareCode, permission: MENU_PERMISSIONS.sqlToQuestion },
      { href: APP_ROUTES.history, labelKey: "nav.history", icon: History, permission: MENU_PERMISSIONS.history },
    ],
  },
  {
    titleKey: "nav.section.prepare",
    defaultCollapsed: true,
    items: [
      // DB 管理チャット（#2264）。全種類の SQL を生成し、SELECT 以外は実行確認語で実行する。
      {
        href: APP_ROUTES.dbAdminChat,
        labelKey: "nav.dbAdminChat",
        icon: BotMessageSquare,
        permission: MENU_PERMISSIONS.dbAdminChat,
      },
      {
        href: APP_ROUTES.adminSql,
        labelKey: "nav.adminSql",
        icon: SquareTerminal,
        permission: MENU_PERMISSIONS.adminSql,
      },
      { href: APP_ROUTES.tableManagement, labelKey: "nav.tableManagement", icon: Table2, permission: MENU_PERMISSIONS.tableManagement },
      { href: APP_ROUTES.viewManagement, labelKey: "nav.viewManagement", icon: Eye, permission: MENU_PERMISSIONS.viewManagement },
      { href: APP_ROUTES.dataManagement, labelKey: "nav.dataManagement", icon: FileSpreadsheet, permission: MENU_PERMISSIONS.dataManagement },
      { href: APP_ROUTES.commentManagement, labelKey: "nav.commentManagement", icon: MessageSquareText, permission: MENU_PERMISSIONS.commentManagement },
      { href: APP_ROUTES.annotationManagement, labelKey: "nav.annotationManagement", icon: Tags, permission: MENU_PERMISSIONS.annotationManagement },
      { href: APP_ROUTES.domainManagement, labelKey: "nav.domainManagement", icon: Shapes, permission: MENU_PERMISSIONS.domainManagement },
      { href: APP_ROUTES.glossaryRules, labelKey: "nav.glossaryRules", icon: BookA, permission: MENU_PERMISSIONS.glossaryRules },
      { href: APP_ROUTES.globalRules, labelKey: "nav.globalRules", icon: ScrollText, permission: MENU_PERMISSIONS.globalRules },
      { href: APP_ROUTES.sampleData, labelKey: "nav.sampleData", icon: Boxes, permission: MENU_PERMISSIONS.sampleData },
    ],
  },
  {
    titleKey: "nav.section.improve",
    defaultCollapsed: true,
    items: [
      { href: APP_ROUTES.profiles, labelKey: "nav.profiles", icon: BriefcaseBusiness, permission: MENU_PERMISSIONS.profiles },
      { href: APP_ROUTES.ontologyBuild, labelKey: "nav.ontologyBuild", icon: Network, permission: MENU_PERMISSIONS.ontologyBuild },
      { href: APP_ROUTES.feedbackManagement, labelKey: "nav.feedbackManagement", icon: ThumbsUp, permission: MENU_PERMISSIONS.feedbackManagement },
      { href: APP_ROUTES.questionClassifierModels, labelKey: "nav.questionClassifierModels", icon: BrainCircuit, permission: MENU_PERMISSIONS.questionClassifierModels },
      { href: APP_ROUTES.evaluation, labelKey: "nav.evaluation", icon: FlaskConical, permission: MENU_PERMISSIONS.evaluation },
    ],
  },
  {
    // NL2SQL 固有のセキュリティ（ロールへの権限付与と DeepSec。#206）。
    titleKey: "nav.section.security",
    defaultCollapsed: true,
    items: [
      { href: APP_ROUTES.securityPermissions, labelKey: "nav.securityPermissions", icon: LockKeyhole, permission: MENU_PERMISSIONS.securityPermissions },
      { href: APP_ROUTES.securityDeepSec, labelKey: "nav.securityDeepSec", icon: ShieldCheck, permission: MENU_PERMISSIONS.securityDeepSec },
    ],
  },
  {
    // 3製品で共通のユーザー管理・ロール管理（画面は platform の共有パッケージ。#206）。
    titleKey: "nav.section.userRoles",
    defaultCollapsed: true,
    items: [
      ...USER_ROLE_NAV_ITEMS.map((item) => ({
        ...item,
        permission: USER_ROLE_MENU_PERMISSIONS[item.key],
      })),
      // 全員の API キー（外部の MCP クライアント向け。RAG / NL2SQL だけ。#1805）。自分のキーは
      // アカウントのメニューから開く。
      {
        href: API_KEY_ADMIN_NAV_ITEM.href,
        labelKey: "nav.securityApiKeys",
        icon: API_KEY_ADMIN_NAV_ITEM.icon,
        permission: MENU_PERMISSIONS.securityApiKeys,
      },
    ],
  },
  {
    // NL2SQL 固有の運用設定（#81）。
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
        icon: CHAT_ATTACHMENT_SETTINGS_NAV_ITEM.icon,
        permission: MENU_PERMISSIONS.settingsChatAttachments,
      },
      // 回答の注意書き（設定は製品ごと。3 製品で同じ位置。#2449）。
      {
        href: ANSWER_NOTICE_NAV_ITEM.href,
        labelKey: "nav.settingsAnswerNotice",
        icon: ANSWER_NOTICE_NAV_ITEM.icon,
        permission: MENU_PERMISSIONS.settingsAnswerNotice,
      },
      // Select AI Credential はデータベース設定から分けた運用の操作（#658）。
      {
        href: APP_ROUTES.settingsSelectAiCredential,
        labelKey: "nav.settingsSelectAiCredential",
        sidebarLabelKey: "nav.settingsSelectAiCredential.sidebar",
        icon: FileKey,
        permission: MENU_PERMISSIONS.settingsSelectAiCredential,
      },
    ],
  },
  {
    // 3製品で共通のシステム設定（画面は platform の共有パッケージ。#70）。
    titleKey: "nav.section.settings",
    defaultCollapsed: true,
    items: SYSTEM_SETTINGS_NAV_ITEMS.map((item) => ({
      ...item,
      permission: SYSTEM_SETTINGS_MENU_PERMISSIONS[item.key],
    })),
  },
];
