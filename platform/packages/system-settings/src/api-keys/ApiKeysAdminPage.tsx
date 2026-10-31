import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ApiErrorState,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  EmptyState,
  ListToolbar,
  PageBody,
  PageHeader,
  SearchField,
  TableSkeleton,
  TimedLoadingState,
  apiErrorMessage,
  toast,
  useConfirm,
} from "@production-ready/ui";

import { ApiKeysTable } from "./ApiKeysTable";
import { apiKeysTranslator, type ApiKeysMessages } from "./messages";
import { API_KEYS_ADMIN_QUERY_KEY, API_KEYS_QUERY_KEY, type ApiKey, type ApiKeysAdminApi } from "./types";

export interface ApiKeysAdminPageProps {
  api: ApiKeysAdminApi;
  messages?: ApiKeysMessages;
}

function matches(key: ApiKey, query: string): boolean {
  const needle = query.trim().toLocaleLowerCase();
  if (!needle) return true;
  return [key.name, key.owner_display_name, key.token_prefix].some((value) =>
    value.toLocaleLowerCase().includes(needle)
  );
}

/**
 * 全員の API キー（管理の画面。「ユーザーとロール」の下。3 製品共通。#1805）。
 * 一覧と削除だけを持つ（キーの発行は各利用者がアカウントの画面から行う。#1873）。
 */
export function ApiKeysAdminPage({ api, messages }: ApiKeysAdminPageProps) {
  const t = apiKeysTranslator(messages);
  const queryClient = useQueryClient();
  const confirm = useConfirm();
  const keys = useQuery({ queryKey: API_KEYS_ADMIN_QUERY_KEY, queryFn: api.list });
  const [query, setQuery] = useState("");
  const allKeys = keys.data?.keys ?? [];
  const visibleKeys = allKeys.filter((key) => matches(key, query));

  const revoke = useMutation({
    mutationFn: (key: ApiKey) => api.revoke(key.id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: API_KEYS_ADMIN_QUERY_KEY });
      void queryClient.invalidateQueries({ queryKey: API_KEYS_QUERY_KEY });
      toast.success(t("apiKeys.revoked"));
    },
    onError: (error) => toast.error(apiErrorMessage(error, t("apiKeys.operationFailed"))),
  });

  async function confirmRevoke(key: ApiKey) {
    const ok = await confirm({
      title: t("apiKeys.revokeTitle", { name: key.name }),
      description: t("apiKeys.revokeOwnerDescription", {
        owner: key.owner_display_name || t("apiKeys.deletedUser"),
      }),
      confirmLabel: t("apiKeys.revoke"),
      tone: "danger",
    });
    if (ok) revoke.mutate(key);
  }

  return (
    <>
      <PageHeader wide title={t("apiKeys.admin.title")} subtitle={t("apiKeys.admin.subtitle")} />
      <PageBody wide>
        <Card className="min-w-0">
          <CardHeader>
            <CardTitle>{t("apiKeys.list.all")}</CardTitle>
          </CardHeader>
          <CardContent>
            {keys.isLoading ? (
              <TimedLoadingState label={t("apiKeys.list.loading")} testId="api-keys-loading">
                <TableSkeleton columns={6} />
              </TimedLoadingState>
            ) : keys.error ? (
              <ApiErrorState
                error={keys.error}
                fallback={t("apiKeys.list.loadFailed")}
                retryLabel={t("apiKeys.retry")}
                onRetry={() => void keys.refetch()}
              />
            ) : allKeys.length === 0 ? (
              <EmptyState title={t("apiKeys.list.emptyAll")} hint={t("apiKeys.list.emptyAllHint")} />
            ) : (
              <div className="space-y-3">
                <ListToolbar
                  search={
                    <SearchField
                      id="api-key-search"
                      label={t("apiKeys.list.search")}
                      labelHidden
                      placeholder={t("apiKeys.list.search")}
                      value={query}
                      onSearch={setQuery}
                      clearLabel={t("apiKeys.list.clearSearch")}
                      resultCountLabel={t("apiKeys.list.count", { count: visibleKeys.length })}
                    />
                  }
                  // 件数はツールバーの右の 1 か所（「全 N 件」「N 件（全 M 件）」。読み上げは検索欄が行う。#1891）。
                  count={{
                    total: allKeys.length,
                    matched: query.trim() ? visibleKeys.length : undefined,
                    live: false,
                  }}
                  testId="api-key-list-toolbar"
                />
                <ApiKeysTable
                  keys={visibleKeys}
                  t={t}
                  showOwner
                  resetKey={query}
                  pagingKey="apiKeysAdmin"
                  paginationAriaLabel={t("apiKeys.list.pagerLabel")}
                  paginationTestId="api-keys-admin-pagination"
                  onRevoke={(key) => void confirmRevoke(key)}
                  empty={<EmptyState title={t("apiKeys.list.noMatch")} />}
                />
              </div>
            )}
          </CardContent>
        </Card>
      </PageBody>
    </>
  );
}
