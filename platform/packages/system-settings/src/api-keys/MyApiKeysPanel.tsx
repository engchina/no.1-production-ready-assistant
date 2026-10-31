import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { KeySquare } from "lucide-react";
import {
  ApiErrorState,
  Banner,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  CopyButton,
  Disclosure,
  EmptyState,
  Fieldset,
  FormActionBar,
  FormGrid,
  FormStatus,
  ListToolbar,
  OneTimeSecret,
  SelectField,
  TableSkeleton,
  TextField,
  TimedLoadingState,
  apiErrorMessage,
  toast,
  useConfirm,
} from "@production-ready/ui";

import { ApiKeysTable } from "./ApiKeysTable";
import { caCertificateUrl, mcpEndpointRows, mcpRegistrationExample } from "./endpoints";
import { apiKeysTranslator, type ApiKeysMessages } from "./messages";
import {
  API_KEY_MAX_PER_USER,
  API_KEY_NAME_MAX_LENGTH,
  API_KEY_PRODUCTS,
  API_KEYS_QUERY_KEY,
  type ApiKey,
  type ApiKeyCreated,
  type ApiKeyProduct,
  type ApiKeyResourceScopes,
  type ApiKeyScopeOption,
  type McpEndpoint,
  type MyApiKeysApi,
} from "./types";

const EXPIRY_OPTIONS = ["30", "90", "365", "none"] as const;
type ExpiryOption = (typeof EXPIRY_OPTIONS)[number];
type AgentScopeMode = "all" | "selected";
// 選択肢のチップ（製品・業務 Agent の範囲。checkbox / radio をラベルで包む）。
const CHOICE_CLASS =
  "flex cursor-pointer items-center gap-2 rounded-md border border-border-control bg-surface px-3 py-2 text-sm text-fg has-[:checked]:border-accent-emphasis has-[:checked]:bg-accent-subtle";

export interface MyApiKeysPanelProps {
  api: MyApiKeysApi;
  /** 今開いている画面から見た MCP の接続先（`mcpEndpoints` で作る）。公開の URL は一覧の応答から足す（#1870）。 */
  endpoints: McpEndpoint[];
  messages?: ApiKeysMessages;
  /**
   * キーで使える業務 Agent を選ばせる候補（#1874。Agent の画面で発行するときに Agent が渡す）。
   * 渡さない製品（RAG / NL2SQL）では、Agent のキーは利用者が使えるすべての業務 Agent になる。
   */
  agentScopeOptions?: ApiKeyScopeOption[];
}

/**
 * 自分の API キー（アカウントの画面の「API キー」のタブ。3 製品共通。#1805・#1873・#1874）。
 * 見出し（`PageHeader`）と余白（`PageBody`）はアカウントの画面が持ち、ここは中身のカードだけを描く。
 *
 * - ログインしている利用者は誰でも、自分のキーを発行・一覧・削除できる（メニュー権限を要しない）。
 * - キーは発行した利用者として動き、利用者の現在のロールで権限とデータの範囲を判定する。
 * - 秘密は発行の直後に 1 回だけ、起点の「発行」の直下に全幅で出す（messaging.md §10.1）。
 * - Agent を選んだキーは、使える業務 Agent を「すべて」か「選んだものだけ」にできる（#1874。最小権限）。
 */
export function MyApiKeysPanel({ api, endpoints, messages, agentScopeOptions }: MyApiKeysPanelProps) {
  const t = apiKeysTranslator(messages);
  const queryClient = useQueryClient();
  const confirm = useConfirm();
  const keys = useQuery({ queryKey: API_KEYS_QUERY_KEY, queryFn: api.list });
  const allKeys = keys.data?.keys ?? [];
  const atLimit = allKeys.length >= API_KEY_MAX_PER_USER;

  const [name, setName] = useState("");
  // 「使える製品」の既定は、どの製品で開いても使える製品のすべて（#1869）。発行の後も同じ既定へ戻す。
  const [products, setProducts] = useState<ApiKeyProduct[]>(() => [...API_KEY_PRODUCTS]);
  const [expiry, setExpiry] = useState<ExpiryOption>("90");
  const [agentScope, setAgentScope] = useState<AgentScopeMode>("all");
  const [agentIds, setAgentIds] = useState<string[]>([]);
  const [submitted, setSubmitted] = useState(false);
  const [created, setCreated] = useState<ApiKeyCreated | null>(null);
  const nameError = submitted && !name.trim() ? t("apiKeys.create.nameRequired") : undefined;
  const productsError = submitted && products.length === 0 ? t("apiKeys.create.productsRequired") : undefined;
  const agentSelected = products.includes("agent");
  const scopingAgents = agentSelected && agentScopeOptions !== undefined && agentScope === "selected";
  const agentScopeError =
    submitted && scopingAgents && agentIds.length === 0 ? t("apiKeys.create.agentScope.required") : undefined;

  const create = useMutation({
    mutationFn: () =>
      api.create({
        name: name.trim(),
        products: API_KEY_PRODUCTS.filter((item) => products.includes(item)),
        expires_in_days: expiry === "none" ? null : (Number(expiry) as 30 | 90 | 365),
        ...(scopingAgents ? { resource_scopes: { agent: agentIds } satisfies ApiKeyResourceScopes } : {}),
      }),
    onSuccess: (result) => {
      setCreated(result);
      setName("");
      setProducts([...API_KEY_PRODUCTS]);
      setAgentScope("all");
      setAgentIds([]);
      setSubmitted(false);
      void queryClient.invalidateQueries({ queryKey: API_KEYS_QUERY_KEY });
    },
  });
  const revoke = useMutation({
    mutationFn: (key: ApiKey) => api.revoke(key.id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: API_KEYS_QUERY_KEY });
      toast.success(t("apiKeys.revoked"));
    },
    onError: (error) => toast.error(apiErrorMessage(error, t("apiKeys.operationFailed"))),
  });

  function submit() {
    setSubmitted(true);
    if (!name.trim() || products.length === 0 || (scopingAgents && agentIds.length === 0)) return;
    create.mutate();
  }

  function toggleProduct(item: ApiKeyProduct, checked: boolean) {
    setProducts((current) => (checked ? [...new Set([...current, item])] : current.filter((value) => value !== item)));
  }

  function toggleAgent(id: string, checked: boolean) {
    setAgentIds((current) => (checked ? [...new Set([...current, id])] : current.filter((value) => value !== id)));
  }

  async function confirmRevoke(key: ApiKey) {
    const ok = await confirm({
      title: t("apiKeys.revokeTitle", { name: key.name }),
      description: t("apiKeys.revokeDescription"),
      confirmLabel: t("apiKeys.revoke"),
      tone: "danger",
    });
    if (ok) revoke.mutate(key);
  }

  return (
    <>
      <McpEndpointsCard endpoints={endpoints} publicBaseUrl={keys.data?.public_base_url ?? ""} t={t} />

      <Card>
        <CardHeader>
          <CardTitle>{t("apiKeys.create.title")}</CardTitle>
          <CardDescription>{t("apiKeys.create.description", { max: API_KEY_MAX_PER_USER })}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {atLimit ? (
            <Banner severity="info">{t("apiKeys.create.limit", { max: API_KEY_MAX_PER_USER })}</Banner>
          ) : null}
          {/* 欄は値の種類の幅（名前 md・有効期限 sm。#1891）。 */}
          <FormGrid>
            <TextField
              id="api-key-name"
              label={t("apiKeys.create.name")}
              width="md"
              required
              value={name}
              maxLength={API_KEY_NAME_MAX_LENGTH}
              placeholder={t("apiKeys.create.namePlaceholder")}
              error={nameError}
              onValueChange={setName}
            />
            <SelectField<ExpiryOption>
              id="api-key-expiry"
              label={t("apiKeys.create.expiry")}
              width="sm"
              value={expiry}
              options={EXPIRY_OPTIONS.map((option) => ({ value: option, label: t(`apiKeys.create.expiry.${option}`) }))}
              onValueChange={setExpiry}
            />
          </FormGrid>
          <Fieldset legend={t("apiKeys.create.products")} required error={productsError}>
            <div className="flex flex-wrap gap-2">
              {API_KEY_PRODUCTS.map((item) => (
                <label key={item} data-surface-tint="accent" className={CHOICE_CLASS}>
                  {/* 製品の複数選択。選択肢が 3 つだけなので、ネイティブの checkbox をラベルで包む。 */}
                  <input
                    type="checkbox"
                    name="api-key-products"
                    value={item}
                    checked={products.includes(item)}
                    onChange={(event) => toggleProduct(item, event.currentTarget.checked)}
                    className="accent-accent-emphasis"
                    data-testid={`api-key-product-${item}`}
                  />
                  {t(`apiKeys.product.${item}`)}
                </label>
              ))}
            </div>
          </Fieldset>
          {agentSelected && agentScopeOptions !== undefined ? (
            <div className="grid gap-3">
              <Fieldset role="radiogroup" legend={t("apiKeys.create.agentScope")} helper={t("apiKeys.create.agentScope.hint")}>
                <div className="flex flex-wrap gap-2">
                  {(["all", "selected"] as const).map((mode) => (
                    <label key={mode} data-surface-tint="accent" className={CHOICE_CLASS}>
                      {/* 2 つから 1 つを選ぶ。選択肢が 2 つだけなので、ネイティブの radio をラベルで包む。 */}
                      <input
                        type="radio"
                        name="api-key-agent-scope"
                        value={mode}
                        checked={agentScope === mode}
                        onChange={() => setAgentScope(mode)}
                        className="accent-accent-emphasis"
                        data-testid={`api-key-agent-scope-${mode}`}
                      />
                      {t(`apiKeys.create.agentScope.${mode}`)}
                    </label>
                  ))}
                </div>
              </Fieldset>
              {agentScope === "selected" ? (
                <Fieldset legend={t("apiKeys.create.agentScope.selected")} required error={agentScopeError}>
                  {agentScopeOptions.length === 0 ? (
                    <p className="text-sm text-fg-muted">{t("apiKeys.create.agentScope.empty")}</p>
                  ) : (
                    <div className="flex flex-wrap gap-2" data-testid="api-key-agent-options">
                      {agentScopeOptions.map((option) => (
                        <label key={option.id} data-surface-tint="accent" className={CHOICE_CLASS}>
                          {/* 業務 Agent の複数選択。件数が少ないので、ネイティブの checkbox をラベルで包む。 */}
                          <input
                            type="checkbox"
                            name="api-key-agent-ids"
                            value={option.id}
                            checked={agentIds.includes(option.id)}
                            onChange={(event) => toggleAgent(option.id, event.currentTarget.checked)}
                            className="accent-accent-emphasis"
                            data-testid={`api-key-agent-${option.id}`}
                          />
                          <span className="min-w-0 break-words">{option.name}</span>
                        </label>
                      ))}
                    </div>
                  )}
                </Fieldset>
              ) : null}
            </div>
          ) : agentSelected ? (
            <p className="text-xs leading-5 text-fg-muted" data-testid="api-key-agent-scope-elsewhere">
              {t("apiKeys.create.agentScope.elsewhere")}
            </p>
          ) : null}
          <FormActionBar
            ariaLabel={t("apiKeys.create.title")}
            primaryActions={[
              {
                id: "create",
                label: t("apiKeys.create.submit"),
                icon: KeySquare,
                loading: create.isPending,
                disabled: atLimit,
                onClick: submit,
                testId: "api-key-create",
              },
            ]}
            status={
              create.error ? (
                <FormStatus tone="danger" message={apiErrorMessage(create.error, t("apiKeys.operationFailed"))} />
              ) : null
            }
          />
          {created ? (
            <OneTimeSecret
              key={created.token}
              id="api-key-created-token"
              title={t("apiKeys.created.title")}
              description={t("apiKeys.created.description")}
              label={t("apiKeys.created.label")}
              value={created.token}
              copyLabel={t("apiKeys.created.copy")}
              copiedMessage={t("apiKeys.created.copied")}
              copyFailedMessage={t("apiKeys.created.copyFailed")}
              doneLabel={t("apiKeys.created.done")}
              onDone={() => setCreated(null)}
              testId="api-key-created"
              valueTestId="api-key-token"
            />
          ) : null}
        </CardContent>
      </Card>

      <Card className="min-w-0">
        <CardHeader>
          <CardTitle>{t("apiKeys.list.mine")}</CardTitle>
        </CardHeader>
        <CardContent>
          {keys.isLoading ? (
            <TimedLoadingState label={t("apiKeys.list.loading")} testId="api-keys-loading">
              <TableSkeleton columns={5} />
            </TimedLoadingState>
          ) : keys.error ? (
            <ApiErrorState
              error={keys.error}
              fallback={t("apiKeys.list.loadFailed")}
              retryLabel={t("apiKeys.retry")}
              onRetry={() => void keys.refetch()}
            />
          ) : allKeys.length === 0 ? (
            <EmptyState title={t("apiKeys.list.empty")} hint={t("apiKeys.list.emptyHint")} />
          ) : (
            <div className="grid min-w-0 gap-2">
              {/* 件数はツールバーの右の 1 か所（「全 N 件」。#1891）。 */}
              <ListToolbar count={{ total: allKeys.length }} testId="my-api-key-list-toolbar" />
              <ApiKeysTable
                keys={allKeys}
                t={t}
                showOwner={false}
                onRevoke={(key) => void confirmRevoke(key)}
                empty={<EmptyState title={t("apiKeys.list.empty")} hint={t("apiKeys.list.emptyHint")} />}
                pagingKey="myApiKeys"
                paginationAriaLabel={t("apiKeys.list.pagerLabel")}
                paginationTestId="my-api-keys-pagination"
              />
            </div>
          )}
        </CardContent>
      </Card>
    </>
  );
}

function McpEndpointsCard({
  endpoints,
  publicBaseUrl,
  t,
}: {
  endpoints: McpEndpoint[];
  publicBaseUrl: string;
  t: ReturnType<typeof apiKeysTranslator>;
}) {
  const rows = mcpEndpointRows(endpoints, publicBaseUrl);
  const example = mcpRegistrationExample(rows, {
    keyComment: t("apiKeys.example.keyComment"),
    timeoutComment: t("apiKeys.example.timeoutComment"),
    caComment: t("apiKeys.example.caComment"),
  });
  const caUrl =
    rows.flatMap((row) => [row.publicUrl, row.localUrl]).map((url) => (url ? caCertificateUrl(url) : null)).find(Boolean) ??
    null;
  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("apiKeys.endpoint.title")}</CardTitle>
        <CardDescription>{t("apiKeys.endpoint.description")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <dl className="grid gap-4">
          {rows.map((row) => {
            const productLabel = t(`apiKeys.product.${row.product}`);
            // この画面の URL と公開の URL が同じなら 1 行（#1870）。
            const lines: { kind: "local" | "public" | "same"; url: string }[] =
              row.localUrl && row.localUrl === row.publicUrl
                ? [{ kind: "same", url: row.localUrl }]
                : [
                    ...(row.localUrl ? [{ kind: "local" as const, url: row.localUrl }] : []),
                    ...(row.publicUrl ? [{ kind: "public" as const, url: row.publicUrl }] : []),
                  ];
            return (
              <div key={row.product} className="grid min-w-0 gap-2" data-testid={`api-keys-endpoint-${row.product}`}>
                <dt className="text-sm font-medium text-fg">{productLabel}</dt>
                {lines.map((line) => {
                  const kindLabel = t(`apiKeys.endpoint.${line.kind}`);
                  return (
                    <dd key={line.kind} className="grid min-w-0 gap-1">
                      <span className="text-xs text-fg-muted">{kindLabel}</span>
                      <span className="flex min-w-0 flex-wrap items-center gap-2">
                        <code
                          className="min-w-0 break-all rounded-md bg-surface-sunken px-3 py-2 font-mono text-sm text-fg"
                          data-testid={`api-keys-endpoint-${row.product}-${line.kind}`}
                        >
                          {line.url}
                        </code>
                        <CopyButton
                          text={line.url}
                          label={t("apiKeys.endpoint.copy")}
                          aria-label={t("apiKeys.endpoint.copyLabel", { product: productLabel, kind: kindLabel })}
                          copiedMessage={t("apiKeys.endpoint.copied")}
                          failedMessage={t("apiKeys.endpoint.copyFailed")}
                        />
                      </span>
                    </dd>
                  );
                })}
              </div>
            );
          })}
        </dl>
        {caUrl ? (
          <p className="break-words text-xs leading-5 text-fg-muted" data-testid="api-keys-ca-note">
            {t("apiKeys.endpoint.caNote")} <code className="break-all font-mono">{caUrl}</code>
          </p>
        ) : null}
        <Disclosure summary={t("apiKeys.endpoint.example")} surface="sunken">
          <div className="grid min-w-0 gap-2">
            <pre className="overflow-x-auto whitespace-pre font-mono text-xs leading-5 text-fg" data-testid="api-keys-example">
              {example}
            </pre>
            <p className="text-xs leading-5 text-fg-muted">{t("apiKeys.endpoint.exampleNote")}</p>
          </div>
        </Disclosure>
      </CardContent>
    </Card>
  );
}
