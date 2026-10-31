import type { FormEvent } from "react";
import { KeyRound, KeySquare } from "lucide-react";
import { useSearchParams } from "react-router-dom";
import {
  Banner,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  FormActionBar,
  PageBody,
  PageHeader,
  TabPanel,
  Tabs,
} from "@production-ready/ui";

import { MyApiKeysPanel } from "../api-keys/MyApiKeysPanel";
import type { ApiKeysMessages } from "../api-keys/messages";
import type { ApiKeyScopeOption, McpEndpoint, MyApiKeysApi } from "../api-keys/types";
import { PasswordChangeFields, usePasswordChangeForm } from "../auth/AuthPages";
import { useAuth } from "../auth/AuthProvider";
import { AUTH_MESSAGES, formatMessage, type AuthMessages } from "../auth/messages";
import { sidebarAccountName, sidebarRoleNames } from "../auth/RequireAuth";
import { ACCOUNT_TABS, type AccountTab } from "../paths";

const TAB_ID_PREFIX = "account";

export interface AccountPageProps {
  /** ログインの画面（パスワードを変えた後に移る）。 */
  loginRoute: string;
  /**
   * 自分の API キー（外部の MCP クライアント向け。3 製品。#1805・#1874）。`agentScopeOptions` は Agent の画面が
   * 渡す業務 Agent の選択肢（キーを業務 Agent に絞る）。
   */
  apiKeys: {
    api: MyApiKeysApi;
    endpoints: McpEndpoint[];
    messages?: ApiKeysMessages;
    agentScopeOptions?: ApiKeyScopeOption[];
  };
  /** パスワードの変更の失敗の表示文言（製品の API の失敗の説明）。 */
  describePasswordError?: (cause: unknown) => string | undefined;
  messages?: Partial<AuthMessages>;
}

function isAccountTab(value: string | null): value is AccountTab {
  return ACCOUNT_TABS.includes(value as AccountTab);
}

/**
 * アカウントの画面（3 製品で同じ。#1873）。サイドナビの下部のアカウントの欄から開く。
 *
 * - 見出しは「アカウント」、説明に名前とロール。タブ（`?tab=api-keys` / `?tab=password`）は URL に残す
 *   （戻る・直接開く・再読み込み）。
 * - 「API キー」: 自分の API キーの発行・一覧・削除（`MyApiKeysPanel`）。
 * - 「パスワード変更」: ログインしている利用者の任意の変更。ローカルの認証（ログインの省略）と、パスワードを
 *   アプリで変えられないアカウントは説明だけを出す。初回・リセット後の強制の変更は単独の画面のまま。
 */
export function AccountPage({ loginRoute, apiKeys, describePasswordError, messages }: AccountPageProps) {
  const m = { ...AUTH_MESSAGES, ...messages };
  const auth = useAuth();
  const [searchParams, setSearchParams] = useSearchParams();
  const requested = searchParams.get("tab");
  const tab: AccountTab = isAccountTab(requested) ? requested : "api-keys";
  const user = auth.user;
  const name = user ? sidebarAccountName(user) : "";
  const roles = user ? sidebarRoleNames(user, m.sidebarRoleSeparator) : "";

  const changeTab = (next: string) => {
    if (!isAccountTab(next)) return;
    setSearchParams(
      (params) => {
        params.set("tab", next);
        return params;
      },
      { replace: true }
    );
  };

  return (
    <>
      <PageHeader
        wide
        title={m.accountTitle}
        subtitle={user ? formatMessage(m.accountSubtitle, { name, roles }) : undefined}
        tabs={
          <Tabs
            idPrefix={TAB_ID_PREFIX}
            ariaLabel={m.accountTabsLabel}
            value={tab}
            onChange={changeTab}
            items={[
              { id: "api-keys", label: m.accountTabApiKeys, icon: KeySquare },
              { id: "password", label: m.accountTabPassword, icon: KeyRound },
            ]}
          />
        }
      />
      <PageBody wide>
        <TabPanel idPrefix={TAB_ID_PREFIX} id="api-keys" value={tab} className="grid content-start gap-6">
          <MyApiKeysPanel
            api={apiKeys.api}
            endpoints={apiKeys.endpoints}
            messages={apiKeys.messages}
            agentScopeOptions={apiKeys.agentScopeOptions}
          />
        </TabPanel>
        <TabPanel idPrefix={TAB_ID_PREFIX} id="password" value={tab} className="grid content-start gap-6">
          {user?.debug_mode ? (
            <div data-testid="account-password-local">
              <Banner severity="info">{m.accountPasswordLocal}</Banner>
            </div>
          ) : user?.password_change_allowed === false ? (
            <div data-testid="account-password-not-allowed">
              <Banner severity="warning">{m.passwordNotAllowed}</Banner>
            </div>
          ) : (
            <PasswordChangeCard loginRoute={loginRoute} messages={messages} describeError={describePasswordError} />
          )}
        </TabPanel>
      </PageBody>
    </>
  );
}

function PasswordChangeCard({
  loginRoute,
  messages,
  describeError,
}: {
  loginRoute: string;
  messages?: Partial<AuthMessages>;
  describeError?: (cause: unknown) => string | undefined;
}) {
  const m = { ...AUTH_MESSAGES, ...messages };
  const form = usePasswordChangeForm({ loginRoute, messages, describeError });
  const handleSubmit = (event: FormEvent) => {
    event.preventDefault();
    void form.submit();
  };
  return (
    <Card>
      <CardHeader>
        <CardTitle>{m.passwordTitle}</CardTitle>
        <CardDescription>{m.accountPasswordDescription}</CardDescription>
      </CardHeader>
      <CardContent>
        {/* 未入力は送信時に検証する（noValidate）。入力欄は 1 列（狭い画面でも同じ並び）。 */}
        <form className="grid max-w-xl gap-4" onSubmit={handleSubmit} noValidate data-testid="account-password-form">
          {form.error ? <Banner severity="danger">{form.error}</Banner> : null}
          <Banner severity="info">{m.passwordRule}</Banner>
          <PasswordChangeFields form={form} messages={messages} size="md" width="md" />
          <FormActionBar
            ariaLabel={m.passwordTitle}
            primaryActions={[
              {
                id: "change-password",
                label: m.passwordSubmit,
                icon: KeyRound,
                loading: form.busy,
                type: "submit",
                testId: "account-password-submit",
              },
            ]}
          />
        </form>
      </CardContent>
    </Card>
  );
}
