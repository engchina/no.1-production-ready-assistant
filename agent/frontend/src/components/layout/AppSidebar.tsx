import { useMemo } from "react";
import { Link, useLocation } from "react-router-dom";
import { SidebarAccountSection } from "@production-ready/system-settings";

import {
  Sidebar as UiSidebar,
  type NavSection as UiNavSection,
  type SidebarLabels,
} from "@production-ready/ui";

import { useAuth } from "@/components/security/AuthProvider";
import { t } from "@/lib/i18n";
import { confirmPendingLeave } from "@/lib/leave-guard";
import { APP_ROUTES } from "@/lib/routes";
import { useUiStore } from "@/lib/ui-store";
import { visibleNavSections } from "./nav-config";

/**
 * Agent コンソールのサイドナビ。共有 UI パッケージの <Sidebar> に
 * i18n / router / 権限 / 状態ストア / nav 構成を注入する（RAG / NL2SQL と同一パターン）。
 * 権限のない項目と、項目が 0 件になったセクションは出さない（#215）。
 */
export function AppSidebar() {
  const { pathname } = useLocation();
  const { hasPermission } = useAuth();
  const collapsed = useUiStore((state) => state.sidebarCollapsed);
  const toggleSidebarCollapsed = useUiStore((state) => state.toggleSidebarCollapsed);
  const collapsedSections = useUiStore((state) => state.collapsedSections);
  const setSectionCollapsed = useUiStore((state) => state.setSectionCollapsed);

  const sections = useMemo<UiNavSection[]>(
    () =>
      visibleNavSections(hasPermission).map((section) => ({
        key: section.titleKey,
        title: t(section.titleKey),
        collapsible: section.collapsible,
        defaultCollapsed: section.defaultCollapsed,
        items: section.items.map((item) => ({
          href: item.href,
          label: t(item.labelKey),
          sidebarLabel: item.sidebarLabelKey ? t(item.sidebarLabelKey) : undefined,
          icon: item.icon,
        })),
      })),
    [hasPermission]
  );

  const labels: SidebarLabels = {
    aria: t("nav.sidebar.aria"),
    expand: t("nav.sidebar.expand"),
    collapse: t("nav.sidebar.collapse"),
    sectionContainsActive: t("nav.section.containsActive"),
    sectionToggleExpand: (section) => t("nav.section.toggle.expand", { section }),
    sectionToggleCollapse: (section) => t("nav.section.toggle.collapse", { section }),
  };

  // 表示名・ロール・パスワード変更・ログアウト（ログイン省略時はその表示）は共通の部品が出す（#307）。
  // ログアウトは画面を離れる操作なので、未保存の編集があれば先に確認する。
  const footer = (
    <SidebarAccountSection
      // 欄（名前・ロール）からアカウントの画面（API キー / パスワード変更のタブ。#1873）を開く。
      routes={{ login: APP_ROUTES.login, account: APP_ROUTES.account }}
      collapsed={collapsed}
      confirmLeave={confirmPendingLeave}
    />
  );

  return (
    <UiSidebar
      sections={sections}
      currentPath={pathname}
      title={{
        line1: t("app.sidebarTitle.line1"),
        line2: t("app.sidebarTitle.line2"),
        full: t("app.title"),
      }}
      collapsed={collapsed}
      onToggleCollapsed={toggleSidebarCollapsed}
      collapsedSections={collapsedSections}
      onSetSectionCollapsed={setSectionCollapsed}
      linkComponent={Link}
      labels={labels}
      footer={footer}
    />
  );
}
