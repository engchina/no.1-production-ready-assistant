import {
  Banner,
  Button,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  ErrorState,
  FormActionBar,
  FormSkeleton,
  FormStatus,
  PageBody,
  Switch,
  TimedLoadingState,
  toast,
} from "@production-ready/ui";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Waypoints, RefreshCw, Save } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";

import { useSettingsDraftGuard, type DraftGuardMessages } from "../guards/useSettingsDraftGuard";
import { TRACE_RECORDING_SETTINGS_MESSAGES, type TraceRecordingSettingsMessages } from "./messages";
import { TRACE_RECORDING_SETTINGS_QUERY_KEY, type TraceRecordingSettingsApi } from "./types";

export interface TraceRecordingSettingsPageProps {
  /** 製品の API 関数（GET / PUT /api/settings/trace-recording）。 */
  api: TraceRecordingSettingsApi;
  /** API エラーから画面に出すメッセージを取り出す（製品の ApiError など）。undefined なら既定の文言。 */
  errorMessage?: (error: unknown) => string | undefined;
  /** 既定の文言の上書き（製品のチャットの説明など）。 */
  messages?: Partial<TraceRecordingSettingsMessages>;
  /** 離脱確認ダイアログの文言の上書き。 */
  draftGuardMessages?: Partial<DraftGuardMessages>;
}

/** 3製品共通の軌跡の本文記録。既存の運用設定と同じ保存・離脱確認を使う。 */
export function TraceRecordingSettingsPage({
  api,
  errorMessage,
  messages,
  draftGuardMessages,
}: TraceRecordingSettingsPageProps) {
  const m = { ...TRACE_RECORDING_SETTINGS_MESSAGES, ...messages };
  const enabledLabelId = useId();
  const enabledHintId = useId();
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: TRACE_RECORDING_SETTINGS_QUERY_KEY,
    queryFn: ({ signal }) => api.get({ signal }),
  });
  const save = useMutation({
    mutationFn: (enabled: boolean) => api.update({ enabled }),
    onSuccess: (data) => {
      queryClient.setQueryData(TRACE_RECORDING_SETTINGS_QUERY_KEY, data);
    },
  });
  const [enabled, setEnabled] = useState(false);
  const baseline = useRef(false);
  useSettingsDraftGuard(enabled !== baseline.current, save.isPending, draftGuardMessages);

  useEffect(() => {
    if (!query.data) return;
    const previous = baseline.current;
    baseline.current = query.data.enabled;
    // 背景の再取得では、利用者が切り替えていないときだけ更新する。
    setEnabled((current) => (current === previous ? query.data.enabled : current));
  }, [query.data]);

  if (query.isPending) {
    return (
      <PageBody wide>
        <TimedLoadingState
          label={m.loading}
          operationKey="trace-recording-settings"
          testId="settings-trace-recording-loading"
        >
          <FormSkeleton fields={2} />
        </TimedLoadingState>
      </PageBody>
    );
  }

  if (query.isError && !query.data) {
    return (
      <PageBody wide>
        <ErrorState
          message={errorMessage?.(query.error) ?? m.loadError}
          onRetry={() => void query.refetch()}
          retryLabel={m.retry}
        />
      </PageBody>
    );
  }

  const saveError = (save.error && errorMessage?.(save.error)) || m.saveError;
  const limits = [{label: m.bodyLabel, value: m.bodyValue}, {label: m.metadataLabel, value: m.metadataValue}];

  return (
    <PageBody wide>
      {query.isError ? (
        <Banner
          severity="warning"
          className="mb-5"
          action={
            <Button
              type="button"
              variant="secondary"
              size="sm"
              icon={RefreshCw}
              loading={query.isFetching}
              onClick={() => void query.refetch()}
            >
              {m.retry}
            </Button>
          }
        >
          {m.refetchError}
        </Banner>
      ) : null}
      <form
        noValidate
        onSubmit={(event) => {
          event.preventDefault();
          if (save.isPending) return;
          save.mutate(enabled, {
            onSuccess: (saved) => {
              baseline.current = saved.enabled;
              setEnabled(saved.enabled);
              toast.success(m.saved);
            },
          });
        }}
      >
        <fieldset disabled={save.isPending} aria-busy={save.isPending} className="min-w-0">
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Waypoints size={16} className="text-accent-fg" aria-hidden />
                {m.settingsTitle}
              </CardTitle>
              <CardDescription>{m.settingsDescription}</CardDescription>
            </CardHeader>
            <CardContent className="space-y-5">
              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0 space-y-1">
                  <p id={enabledLabelId} className="text-sm font-medium text-fg">
                    {m.enabledLabel}
                  </p>
                  <p id={enabledHintId} className="text-xs leading-relaxed text-fg-muted">
                    {m.enabledHint}
                  </p>
                </div>
                <Switch
                  checked={enabled}
                  aria-labelledby={enabledLabelId}
                  aria-describedby={enabledHintId}
                  onCheckedChange={(value) => {
                    if (save.isPending) return;
                    setEnabled(value);
                    save.reset();
                  }}
                  data-testid="trace-recording-enabled"
                />
              </div>
              <section className="space-y-2" aria-labelledby="trace-recording-scope-title">
                <div className="space-y-1">
                  <h3 id="trace-recording-scope-title" className="text-sm font-medium text-fg">
                    {m.scopeTitle}
                  </h3>
                  <p className="text-xs text-fg-muted">{m.scopeDescription}</p>
                </div>
                <dl className="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-[auto_1fr]" data-testid="trace-recording-scope">
                  {limits.map((item) => (
                    <div key={item.label} className="contents">
                      <dt className="text-fg-muted">{item.label}</dt>
                      <dd className="text-fg">{item.value}</dd>
                    </div>
                  ))}
                </dl>
              </section>
              <p className="text-xs leading-relaxed text-fg-muted">{m.historyHint}</p>
              <FormActionBar
                ariaLabel={m.actionsLabel}
                primaryActions={[{ id: "save", type: "submit", label: m.save, icon: Save, loading: save.isPending }]}
                status={save.isError ? <FormStatus tone="danger" message={saveError} /> : null}
              />
            </CardContent>
          </Card>
        </fieldset>
      </form>
    </PageBody>
  );
}
