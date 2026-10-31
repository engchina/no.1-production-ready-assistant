import { useMemo } from "react";
import { Link, useLocation } from "react-router-dom";
import { SidebarAccountSection } from "@production-ready/system-settings";

import {
  Sidebar as UiSidebar,
  type NavSection as UiNavSection,
  type SidebarLabels,
} from "@production-ready/ui";

import { t } from "@/lib/i18n";
import { APP_ROUTES } from "@/lib/routes";
import { useUiStore } from "@/lib/ui-store";
import { useAuth } from "@/features/security/AuthProvider";
import { NAV_SECTIONS } from "./nav-config";

/**
 * NL2SQL コンソールのサイドナビ。共有 UI パッケージの <Sidebar> に
 * i18n / router / 状態ストア / nav 構成を注入する NL2SQL shell。
 */
export function AppSidebar() {
  const { pathname } = useLocation();
  const auth = useAuth();
  const collapsed = useUiStore((state) => state.sidebarCollapsed);
  const toggleSidebarCollapsed = useUiStore((state) => state.toggleSidebarCollapsed);
  // 開閉の保存値。保存値の無いセクションは nav config の defaultCollapsed に従う（共有 Sidebar。#1563）。
  const collapsedSections = useUiStore((state) => state.collapsedSections);
  const setSectionCollapsed = useUiStore((state) => state.setSectionCollapsed);

  const sections = useMemo<UiNavSection[]>(
    () =>
      NAV_SECTIONS.map((section) => ({
        key: section.titleKey,
        title: t(section.titleKey),
        collapsible: section.collapsible,
        defaultCollapsed: section.defaultCollapsed,
        items: section.items.filter((item) => auth.hasPermission(item.permission)).map((item) => ({
          href: item.href,
          label: t(item.labelKey),
          sidebarLabel: item.sidebarLabelKey ? t(item.sidebarLabelKey) : undefined,
          icon: item.icon,
        })),
      })).filter((section) => section.items.length > 0),
    [auth]
  );

  const labels: SidebarLabels = {
    aria: t("nav.sidebar.aria"),
    expand: t("nav.sidebar.expand"),
    collapse: t("nav.sidebar.collapse"),
    sectionContainsActive: t("nav.section.containsActive"),
    sectionToggleExpand: (section) => t("nav.section.toggle.expand", { section }),
    sectionToggleCollapse: (section) => t("nav.section.toggle.collapse", { section }),
  };

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
      // ログイン ID・ロール名・ログアウト（ログイン省略時はその表示）は共通の部品が出す（#307・#1696）。欄（名前・ロール）から
      // アカウントの画面（API キー / パスワード変更のタブ。#1873）を開く。
      footer={
        <SidebarAccountSection
          routes={{ login: APP_ROUTES.login, account: APP_ROUTES.account }}
          collapsed={collapsed}
        />
      }
    />
  );
}

