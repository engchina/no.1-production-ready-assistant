"use client";

import {
  PageBody, Card, CardContent, CardDescription, CardHeader, CardTitle,
  Fieldset, FormActionBar, FormStatus, TimedLoadingState, FormSkeleton, ButtonLink,
} from "@production-ready/ui";
import { useState } from "react";
import { RotateCcw, Save, ShieldCheck } from "lucide-react";

import { ApiErrorState } from "@/components/StateViews";
import { ApiError } from "@/lib/api";
import { useLeaveGuard } from "@/lib/leave-guard";
import { useValuesChanged } from "@/lib/render-sync";
import { t } from "@/lib/i18n";
import { useGuardrailSettings, useUpdateGuardrailSettings } from "@/lib/queries";
import { APP_ROUTES } from "@/lib/routes";
import { toast } from "@/lib/toast";


/** 全体の OCI Guardrails の有効・無効を管理する。 */
export function GuardrailSettingsClient() {
  const query = useGuardrailSettings();
  const save = useUpdateGuardrailSettings();
  const [selection, setSelection] = useState<{ input_enabled: boolean; output_enabled: boolean } | null>(null);
  const serverChanged = useValuesChanged([query.data]);
  if (serverChanged && query.data) setSelection({ input_enabled: query.data.input_enabled, output_enabled: query.data.output_enabled });
  const dirty = selection !== null && query.data !== undefined && (selection.input_enabled !== query.data.input_enabled || selection.output_enabled !== query.data.output_enabled);
  useLeaveGuard(dirty, save.isPending);

  if (query.isPending) return (
    <PageBody wide>
      <TimedLoadingState label={t("settings.loading")} operationKey="settings-guardrail-load" placement="page" testId="settings-guardrail-loading">
        <FormSkeleton />
      </TimedLoadingState>
    </PageBody>
  );
  if (query.isError) return (
    <PageBody wide>
      <ApiErrorState error={query.error} fallback={t("settings.guardrail.loadError")} onRetry={() => void query.refetch()} />
    </PageBody>
  );
  const settings = query.data;
  if (!settings || selection === null) return null;
  const warning = (selection.input_enabled || selection.output_enabled) && !settings.oci_configured
    ? t(settings.oci_warning_code === "oci_guardrails_compartment_missing"
      ? "settings.guardrail.ociWarning.compartmentMissing"
      : "settings.guardrail.ociWarning.credentialsInvalid")
    : null;

  return (
    <PageBody wide>
      <Card>
        <CardHeader>
          <div className="flex items-start gap-3">
            <div data-surface-tint="info" className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md bg-info-subtle text-info-fg">
              <ShieldCheck size={20} aria-hidden />
            </div>
            <div className="min-w-0">
              <CardTitle>{t("settings.guardrail.ociLabel")}</CardTitle>
              <CardDescription>{t("settings.guardrail.ociDescription")}</CardDescription>
            </div>
          </div>
        </CardHeader>
        <CardContent className="space-y-5">
          <Fieldset legend={t("settings.guardrail.selection")}>
            <div className="space-y-3">
              {(["input", "output"] as const).map((side) => (
                <div key={side}>
                  <label className="flex cursor-pointer items-center gap-2 rounded-md border border-border px-3 py-3 text-sm text-fg">
                    <input type="checkbox" aria-label={t(`settings.guardrail.${side}`)}
                      aria-describedby={`guardrail-${side}-description`}
                      className="accent-accent-emphasis focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring"
                      checked={selection[`${side}_enabled`]} disabled={save.isPending}
                      onChange={(event) => { save.reset(); setSelection({ ...selection, [`${side}_enabled`]: event.target.checked }); }} />
                    {t(`settings.guardrail.${side}`)}
                  </label>
                  <p id={`guardrail-${side}-description`} className="mt-1 text-sm text-fg-muted">{t(`settings.guardrail.${side}.description`)}</p>
                </div>
              ))}
            </div>
          </Fieldset>
          <p className="text-sm leading-relaxed text-fg-muted">{t("settings.guardrail.resultHandling")}</p>
          <p>
            <a href="https://docs.oracle.com/en-us/iaas/Content/generative-ai/guardrails.htm" target="_blank" rel="noopener noreferrer" className="text-sm text-accent-fg underline underline-offset-2">
            {t("settings.guardrail.officialDocs")}
            </a>
          </p>
          <FormStatus tone="info" message={t("settings.guardrail.overview.description")} />
          {warning ? (
            <div className="space-y-2">
              <FormStatus tone="warning" message={warning} />
              <ButtonLink to={APP_ROUTES.settingsOci} variant="secondary">{t("settings.guardrail.ociSettingsLink")}</ButtonLink>
            </div>
          ) : null}
          <FormActionBar ariaLabel={t("settings.guardrail.actions.label")}
            primaryActions={[{ id: "save", label: t("settings.guardrail.actions.save"), icon: Save, loading: save.isPending, disabled: !dirty,
              onClick: () => save.mutate(selection, { onSuccess: (data) => { setSelection({ input_enabled: data.input_enabled, output_enabled: data.output_enabled }); toast.success(t("settings.guardrail.actions.saved")); } }),
            }]}
            secondaryActions={[{ id: "reset", label: t("settings.guardrail.actions.reset"), icon: RotateCcw, disabled: !dirty || save.isPending,
              onClick: () => { save.reset(); setSelection({ input_enabled: settings.input_enabled, output_enabled: settings.output_enabled }); },
            }]}
            status={save.isError ? <FormStatus tone="danger" message={save.error instanceof ApiError ? save.error.message : t("settings.guardrail.saveError")} />
              : dirty ? <FormStatus tone="warning" message={t("settings.guardrail.actions.unsaved")} /> : null}
          />
        </CardContent>
      </Card>
    </PageBody>
  );
}
