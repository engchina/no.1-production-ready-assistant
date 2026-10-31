import { useQuery } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import {
  ApiErrorBanner,
  Banner,
  Button,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  FormSkeleton,
  SearchableMultiSelect,
  Section,
  SelectField,
  StatusBadge,
  TimedLoadingState,
  type SearchableSelectOption,
} from "@production-ready/ui";
import {
  agentApi,
  ApiError,
  type AgentDataScope,
  type AgentDataScopeConnection,
  type AgentDataScopes,
  type DataScopeCandidate,
} from "@/lib/api";
import { t, type I18nKey } from "@/lib/i18n";

/** データの範囲を持てる接続（画面に出す順）。 */
export const DATA_SCOPE_CONNECTIONS: readonly AgentDataScopeConnection[] = ["nl2sql", "rag"];

/** 接続ごとの文言の key（業務プロファイル / 検索・回答プロファイル）。 */
const CONNECTION_KEYS: Record<
  AgentDataScopeConnection,
  { title: I18nKey; description: I18nKey; label: I18nKey; defaultLabel: I18nKey; loading: I18nKey }
> = {
  nl2sql: {
    title: "agent.dataScope.nl2sql.title",
    description: "agent.dataScope.nl2sql.description",
    label: "agent.dataScope.nl2sql.label",
    defaultLabel: "agent.dataScope.nl2sql.default",
    loading: "agent.dataScope.nl2sql.loading",
  },
  rag: {
    title: "agent.dataScope.rag.title",
    description: "agent.dataScope.rag.description",
    label: "agent.dataScope.rag.label",
    defaultLabel: "agent.dataScope.rag.default",
    loading: "agent.dataScope.rag.loading",
  },
};

/** 比べられる形（ID を並べ替え、一覧が空の接続は持たない）。保存の payload にもこの形を送る。 */
export function normalizeDataScopes(scopes: AgentDataScopes | undefined): AgentDataScopes {
  const normalized: AgentDataScopes = {};
  for (const connection of DATA_SCOPE_CONNECTIONS) {
    const scope = scopes?.[connection];
    if (!scope || scope.profile_ids.length === 0) continue;
    const ids = [...new Set(scope.profile_ids)].sort();
    const fallback = ids.length === 1 ? ids[0] : "";
    normalized[connection] = {
      profile_ids: ids,
      default_profile_id: ids.includes(scope.default_profile_id) ? scope.default_profile_id : fallback,
    };
  }
  return normalized;
}

/**
 * 業務 Agent のデータの範囲（#1378）。NL2SQL の業務プロファイルと RAG の検索・回答プロファイルを、
 * 接続ごとに複数選んで既定を 1 つ決める。選ばない接続は「範囲なし」（利用者が使えるすべてから実行時に選ぶ）。
 * 候補は編集者として RAG / NL2SQL に問い合わせる（編集者が使えるものだけが出る）。
 */
export function AgentDataScopeSection({
  fieldId,
  value,
  onChange,
  readOnly,
  disabled,
}: {
  fieldId: string;
  value: AgentDataScopes;
  onChange: (value: AgentDataScopes) => void;
  /** 変更の権限が無い利用者は候補を問い合わせず、選んだ ID を見せるだけ。 */
  readOnly: boolean;
  disabled: boolean;
}) {
  return (
    <Section title={t("agent.dataScope.title")} description={t("agent.dataScope.description")}>
      {/* wide の画面では 2 つの接続を横に並べる（README §4）。 */}
      <div className="grid min-w-0 gap-4 xl:grid-cols-2">
        {DATA_SCOPE_CONNECTIONS.map((connection) => (
          <DataScopeCard
            key={connection}
            connection={connection}
            fieldId={`${fieldId}-data-scope-${connection}`}
            scope={value[connection]}
            readOnly={readOnly}
            disabled={disabled}
            onChange={(scope) => {
              const next = { ...value };
              if (scope) next[connection] = scope;
              else delete next[connection];
              onChange(next);
            }}
          />
        ))}
      </div>
    </Section>
  );
}

function DataScopeCard({
  connection,
  fieldId,
  scope,
  readOnly,
  disabled,
  onChange,
}: {
  connection: AgentDataScopeConnection;
  fieldId: string;
  scope: AgentDataScope | undefined;
  readOnly: boolean;
  disabled: boolean;
  onChange: (scope: AgentDataScope | undefined) => void;
}) {
  const keys = CONNECTION_KEYS[connection];
  const candidates = useQuery({
    queryKey: ["agent-data-scope-candidates", connection],
    queryFn: () => agentApi.listDataScopeCandidates(connection),
    enabled: !readOnly,
    // 接続の設定・権限の失敗は再試行しても変わらない（「再試行」で取り直す）。
    retry: false,
    staleTime: 60_000,
  });
  const forbidden = candidates.error instanceof ApiError && candidates.error.status === 403;
  const ids = scope?.profile_ids ?? [];
  const profiles = candidates.data?.profiles ?? [];
  const known = new Map(profiles.map((profile) => [profile.id, profile]));
  const options = profiles.map(candidateOption);
  // 候補に無い選択済み（編集者が使えない・削除された）も、外せるよう ID を名前にして出す。
  const selectedOptions = ids.map((id) => {
    const profile = known.get(id);
    if (profile) return candidateOption(profile);
    return {
      value: id,
      label: id,
      badge: candidates.isSuccess ? t("agent.dataScope.unavailable") : undefined,
    } satisfies SearchableSelectOption;
  });
  const missing = candidates.isSuccess ? ids.filter((id) => !known.has(id)) : [];

  function setIds(next: string[]) {
    if (next.length === 0) {
      onChange(undefined);
      return;
    }
    const current = scope?.default_profile_id ?? "";
    // 既定が外れたら、選んだ最初のものを既定にする（複数のときは下の選択欄で変えられる）。
    onChange({ profile_ids: next, default_profile_id: next.includes(current) ? current : next[0] });
  }

  const mode = ids.length === 0 ? "unset" : ids.length === 1 ? "single" : "multiple";
  const modeLabel: Record<typeof mode, I18nKey> = {
    unset: "agent.dataScope.mode.unset",
    single: "agent.dataScope.mode.single",
    multiple: "agent.dataScope.mode.multiple",
  };
  const modeHint: Record<typeof mode, I18nKey> = {
    unset: "agent.dataScope.hint.unset",
    single: "agent.dataScope.hint.single",
    multiple: "agent.dataScope.hint.multiple",
  };

  return (
    <Card className="min-w-0" data-testid={`agent-data-scope-${connection}`}>
      <CardHeader>
        <div className="flex min-w-0 flex-wrap items-center justify-between gap-2">
          <CardTitle>{t(keys.title)}</CardTitle>
          {/* 状態は文字でも伝える（色だけに頼らない）。 */}
          <StatusBadge
            variant={mode === "unset" ? "neutral" : "info"}
            label={t(modeLabel[mode], { count: ids.length })}
            icon={false}
          />
        </div>
        <CardDescription>{t(keys.description)}</CardDescription>
      </CardHeader>
      <CardContent className="grid min-w-0 gap-4">
        {candidates.isLoading ? (
          <TimedLoadingState label={t(keys.loading)} testId={`agent-data-scope-${connection}-loading`}>
            <FormSkeleton fields={1} title={false} actions={false} />
          </TimedLoadingState>
        ) : (
          <>
            {forbidden ? (
              <div data-testid={`agent-data-scope-${connection}-forbidden`}>
                <Banner severity="info">{t("agent.dataScope.forbidden", { product: t(keys.title) })}</Banner>
              </div>
            ) : candidates.error ? (
              <ApiErrorBanner
                error={candidates.error}
                fallback={t("agent.dataScope.loadError")}
                testId={`agent-data-scope-${connection}-error`}
                action={
                  <Button
                    variant="secondary"
                    size="sm"
                    icon={RefreshCw}
                    loading={candidates.isFetching}
                    onClick={() => void candidates.refetch()}
                  >
                    {t("common.retry")}
                  </Button>
                }
              />
            ) : null}
            <SearchableMultiSelect
              id={fieldId}
              label={t(keys.label)}
              helper={t(modeHint[mode])}
              // 対象の選択（検索・回答プロファイル / 業務プロファイル）は lg（#1891）。
              width="lg"
              options={options}
              selectedOptions={selectedOptions}
              value={ids}
              onValueChange={setIds}
              disabled={disabled || readOnly || forbidden}
              labels={{
                empty: candidates.error ? t("agent.dataScope.emptyOnError") : t("agent.dataScope.empty"),
                searchPlaceholder: t("agent.dataScope.search"),
              }}
            />
            {missing.length ? (
              <p className="text-xs text-warning-fg" data-testid={`agent-data-scope-${connection}-missing`}>
                {t("agent.dataScope.missing", { ids: missing.join("、") })}
              </p>
            ) : null}
            {ids.length > 1 && scope ? (
              <SelectField
                id={`${fieldId}-default`}
                label={t(keys.defaultLabel)}
                helper={t("agent.dataScope.defaultHint")}
                width="lg"
                required
                disabled={disabled || readOnly}
                value={scope.default_profile_id}
                options={selectedOptions.map((option) => ({ value: option.value, label: option.label }))}
                onValueChange={(next) => onChange({ ...scope, default_profile_id: next })}
                data-testid={`${fieldId}-default`}
              />
            ) : null}
          </>
        )}
      </CardContent>
    </Card>
  );
}

function candidateOption(profile: DataScopeCandidate): SearchableSelectOption {
  return {
    value: profile.id,
    label: profile.name,
    description: profile.description || profile.id,
    searchText: `${profile.name} ${profile.id} ${profile.description}`,
  };
}
