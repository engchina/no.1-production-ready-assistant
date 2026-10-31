import {
  FormActionBar,
  FormSkeleton,
  FormStatus,
  PageBody,
  Card,
  CardHeader,
  CardTitle,
  CardDescription,
  CardContent,
  TimedLoadingState,
} from "@production-ready/ui";
import { CheckCircle2, Tags, RotateCcw, Save } from "lucide-react";
import { useState } from "react";

import { ApiErrorState } from "@/components/StateViews";
import { ApiError } from "@/lib/api";
import { t } from "@/lib/i18n";
import { useLeaveGuard } from "@/lib/leave-guard";
import { useEntityIndexSettings, useUpdateEntityIndexSettings } from "@/lib/queries";
import { useValuesChanged } from "@/lib/render-sync";
import { toast } from "@/lib/toast";
import { cn } from "@/lib/utils";

/** 実体・別名索引を構築する全体の既定。文書レシピの明示設定は変更しない。 */
export function EntityIndexSettingsClient() {
  const query = useEntityIndexSettings();
  const save = useUpdateEntityIndexSettings();
  const [enabled, setEnabled] = useState(false);
  const serverChanged = useValuesChanged([query.data]);
  if (serverChanged && query.data) setEnabled(query.data.enabled);
  const dirty = Boolean(query.data && enabled !== query.data.enabled);
  useLeaveGuard(dirty, save.isPending);

  if (query.isPending) {
    return (
      <PageBody wide>
        <TimedLoadingState label={t("settings.loading")} operationKey="settings-entity-index-load" placement="page">
          <FormSkeleton />
        </TimedLoadingState>
      </PageBody>
    );
  }
  if (query.isError) {
    return (
      <PageBody wide>
        <ApiErrorState error={query.error} fallback={t("settings.entityIndex.error")} onRetry={() => void query.refetch()} />
      </PageBody>
    );
  }

  return (
    <PageBody wide>
      <Card>
        <CardHeader>
          <div className="flex items-start gap-3">
            <div data-surface-tint="info" className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md bg-info-subtle text-info-fg">
              <Tags size={20} aria-hidden />
            </div>
            <div className="min-w-0">
              <CardTitle>{t("settings.entityIndex.title")}</CardTitle>
              <CardDescription>{t("settings.entityIndex.description")}</CardDescription>
            </div>
          </div>
        </CardHeader>
        <CardContent className="space-y-5">
          <div className="space-y-2">
            <div className="text-sm font-medium text-fg" id="entity-index-build-label">
              {t("settings.graph.profile")}
            </div>
            <div role="radiogroup" aria-labelledby="entity-index-build-label" className="grid grid-cols-1 gap-2 md:grid-cols-2">
              {[false, true].map((value) => {
                const selected = enabled === value;
                const name = value ? "on" : "off";
                return (
                  <div key={name} className="relative min-w-0">
                    <input id={`entity-index-build-${name}`} className="peer absolute inset-0 z-10 cursor-pointer opacity-0 disabled:cursor-not-allowed"
                      type="radio" name="entity-index-build" value={name} checked={selected} disabled={save.isPending}
                      onChange={() => { save.reset(); setEnabled(value); }} />
                    <label htmlFor={`entity-index-build-${name}`} data-surface-tint={selected ? "accent" : undefined}
                      className={cn("block h-full cursor-pointer rounded-md border px-3 py-2 text-left transition-colors peer-focus-visible:outline-2 peer-focus-visible:outline-offset-2 peer-focus-visible:outline-focus-ring peer-disabled:cursor-not-allowed peer-disabled:opacity-50",
                        selected ? "border-accent-emphasis bg-accent-subtle text-fg" : "border-border bg-surface text-fg peer-hover:bg-surface-hover")}>
                      <span className="flex items-center justify-between gap-2">
                        <span className="flex flex-wrap items-center gap-2">
                          <span className="text-sm font-semibold">{t(value ? "settings.graph.profile.entities" : "settings.graph.profile.off")}</span>
                          {!value ? <span className="inline-flex min-h-5 items-center rounded border border-border px-1.5 text-xs text-fg-muted">{t("settings.graph.defaultTag")}</span> : null}
                        </span>
                        {selected ? <CheckCircle2 size={16} className="shrink-0 text-accent-fg" aria-hidden /> : null}
                      </span>
                      <span className="mt-1 block text-xs leading-relaxed text-fg-muted">{t(value ? "settings.entityIndex.on.description" : "settings.entityIndex.off.description")}</span>
                    </label>
                  </div>
                );
              })}
            </div>
          </div>
          <FormStatus tone="info" message={t("settings.entityIndex.hint")} />
          <FormActionBar
            ariaLabel={t("settings.entityIndex.actions")}
            primaryActions={[{
              id: "save", label: t("settings.graph.actions.save"), icon: Save,
              loading: save.isPending, disabled: !dirty,
              onClick: () => save.mutate({ enabled }, { onSuccess: () => toast.success(t("settings.entityIndex.saved")) }),
            }]}
            secondaryActions={[{
              id: "reset", label: t("settings.graph.actions.reset"), icon: RotateCcw,
              disabled: !dirty || save.isPending,
              onClick: () => { save.reset(); setEnabled(query.data?.enabled ?? false); },
            }]}
            status={save.isError ? <FormStatus tone="danger" message={save.error instanceof ApiError ? save.error.message : t("settings.entityIndex.error")} /> : dirty ? <FormStatus tone="warning" message={t("settings.graph.actions.unsaved")} /> : null}
          />
        </CardContent>
      </Card>
    </PageBody>
  );
}
