import { Bug } from "lucide-react";
import { SidebarAccountFooter, cn, useSidebarCollapsed } from "@production-ready/ui";

import { useSidebarAccount, type SidebarAccountRoutes } from "./RequireAuth";
import { AUTH_MESSAGES, type AuthMessages } from "./messages";

export interface SidebarAccountSectionProps {
  /** `apiKeys` を渡すと、アカウント欄に自分の API キーの画面を出す（RAG / NL2SQL。#1805）。 */
  routes: SidebarAccountRoutes;
  collapsed: boolean;
  /** ログアウトの前に呼ぶ確認（未保存の編集の確認など）。false で中止する。 */
  confirmLeave?: () => Promise<boolean>;
  messages?: Partial<AuthMessages>;
}

/**
 * サイドバー下部のアカウント欄（3 製品共通。#307）。Sidebar の `footer` に渡す。
 * 利用者名・ロール・パスワード変更・ログアウトを出し、ログイン省略（ローカル DEBUG）では
 * パスワード変更・ログアウトの代わりに「ログイン省略」を示す。未ログインなら何も出さない。
 */
export function SidebarAccountSection({ routes, collapsed: collapsedPreference, confirmLeave, messages }: SidebarAccountSectionProps) {
  const account = useSidebarAccount({ routes, messages });
  // md 未満のドロワーの中では展開して描く（#367）。
  const collapsed = useSidebarCollapsed(collapsedPreference);
  if (!account) return null;
  const logout = account.onLogout;
  const debugLabel = messages?.sidebarDebugMode ?? AUTH_MESSAGES.sidebarDebugMode;
  return (
    <SidebarAccountFooter
      name={account.name}
      roles={account.roles}
      rolesLabel={account.rolesLabel}
      collapsed={collapsed}
      labels={account.labels}
      notice={
        account.debugMode ? (
          <div
            data-surface-tint="warning"
            className={cn(
              "flex min-h-9 items-center gap-2 rounded-md border border-warning-border bg-warning-subtle text-warning-fg",
              collapsed ? "justify-center px-1" : "px-2 py-1.5"
            )}
            role="status"
            aria-label={debugLabel}
            title={collapsed ? debugLabel : undefined}
          >
            <Bug size={16} className="shrink-0" aria-hidden />
            {!collapsed ? <span className="text-xs leading-4">{debugLabel}</span> : null}
          </div>
        ) : undefined
      }
      account={account.account}
      onLogout={
        logout
          ? () => {
              if (!confirmLeave) return logout();
              void confirmLeave().then((confirmed) => {
                if (confirmed) logout();
              });
            }
          : undefined
      }
    />
  );
}
