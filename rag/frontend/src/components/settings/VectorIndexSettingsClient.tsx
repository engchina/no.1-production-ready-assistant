"use client";

import {
  PageBody,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  FormActionBar,
  FormStatus,
  TimedLoadingState,
  FormSkeleton,
} from "@production-ready/ui";
import { useState } from "react";
import { Boxes, CheckCircle2, Database, RotateCcw, Save } from "lucide-react";

import { ApiErrorState } from "@/components/StateViews";
import { SettingsPreviewCard } from "@/components/settings/SettingsPreviewPanels";
import {
  ApiError,
  type VectorIndexBuildStatus,
  type VectorIndexProfileName,
  type VectorIndexProfileStatusData,
  type VectorIndexSettingsData,
} from "@/lib/api";
import { useLeaveGuard } from "@/lib/leave-guard";
import { t, type I18nKey } from "@/lib/i18n";
import { useUpdateVectorIndexSettings, useVectorIndexSettings } from "@/lib/queries";
import { toast } from "@/lib/toast";
import { cn } from "@/lib/utils";

const PROFILE_ORDER: VectorIndexProfileName[] = ["fast", "balanced", "accurate"];

/** 検索インデックスの現在設定を管理する設定画面。 */
export function VectorIndexSettingsClient() {
  const query = useVectorIndexSettings();
  const save = useUpdateVectorIndexSettings();
  const [profile, setProfile] = useState<VectorIndexProfileName | null>(null);
  // 直前に取り込んだ server 値。server 値が変わったレンダーだけ、未編集なら選択をそろえる。
  // 編集中の選択は裏の再取得で上書きせず、編集していなければ今の保存値を出す（#987）。
  // 保存の開始・終了（isPending）では選択を戻さない（失敗しても選択を残す）。
  const [base, setBase] = useState<VectorIndexProfileName | null>(null);
  const serverProfile = query.data?.profile ?? null;
  if (serverProfile !== null && serverProfile !== base) {
    setBase(serverProfile);
    if (profile === null || profile === base) setProfile(serverProfile);
  }

  // 未保存の選択があるときは離脱を確認し、保存中は離脱を止める。
  useLeaveGuard(
    Boolean(query.data && profile !== null && profile !== query.data.profile),
    save.isPending
  );

  if (query.isPending) {
    return (
      <PageBody wide>
        <TimedLoadingState
          label={t("settings.loading")}
          operationKey="settings-vector-index-load"
          placement="page"
          testId="settings-vector-index-loading"
        >
          <FormSkeleton />
        </TimedLoadingState>
      </PageBody>
    );
  }

  if (query.isError) {
    return (
      <PageBody wide>
        <ApiErrorState
          error={query.error}
          fallback={t("settings.vectorIndex.loadError")}
          onRetry={() => void query.refetch()}
        />
      </PageBody>
    );
  }

  const settings = query.data;
  if (!settings || !profile) return null;

  const dirty = profile !== settings.profile;
  const saveError =
    save.error instanceof ApiError ? save.error.message : t("settings.vectorIndex.saveError");
  const profiles = orderedProfiles(settings.profiles);
  const selectedProfile = profiles.find((item) => item.name === profile);

  function selectProfile(next: VectorIndexProfileName) {
    save.reset();
    setProfile(next);
  }

  function resetForm() {
    save.reset();
    setProfile(settings.profile);
  }

  function submit() {
    if (!profile || save.isPending) return;
    save.mutate(
      { profile },
      {
        onSuccess: (data) => {
          setBase(data.profile);
          setProfile(data.profile);
          // 保存の成功は Toast、失敗は操作の行の FormStatus（messaging.md §10.2）。
          toast.success(t("settings.vectorIndex.actions.saved"));
        },
      }
    );
  }

  // 再作成の要否は backend が実際の索引と比べた判定(#562)をそのまま使う。
  const reprovision = reprovisionStatus(selectedProfile?.index_status);

  return (
    <PageBody wide>
      <Card>
        <CardHeader>
          <div className="flex items-start gap-3">
            <div data-surface-tint="info" className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md bg-info-subtle text-info-fg">
              <Boxes size={20} aria-hidden />
            </div>
            <div>
              <CardTitle>{t("settings.vectorIndex.overview.title")}</CardTitle>
              <CardDescription>{t("settings.vectorIndex.overview.description")}</CardDescription>
            </div>
          </div>
        </CardHeader>
        <CardContent className="space-y-5">
          <div className="space-y-2">
            <div className="text-sm font-medium text-fg">
              {t("settings.vectorIndex.profile")}
            </div>
            <div
              role="radiogroup"
              aria-label={t("settings.vectorIndex.profile")}
              className="grid grid-cols-1 gap-2 md:grid-cols-3"
            >
              {profiles.map((item) => {
                const selected = profile === item.name;
                return (
                  <div key={item.name} className="relative min-w-0">
                    <input
                      id={`settings-vector-index-profile-${item.name}`}
                      className="peer absolute inset-0 z-10 cursor-pointer opacity-0 disabled:cursor-not-allowed"
                      type="radio"
                      name="settings-vector-index-profile"
                      value={item.name}
                      checked={selected}
                      disabled={save.isPending}
                      onChange={() => selectProfile(item.name)}
                    />
                    <label
                      data-surface-tint={selected ? "accent" : undefined}
                      htmlFor={`settings-vector-index-profile-${item.name}`}
                      className={cn(
                        "block h-full cursor-pointer min-h-[8rem] rounded-md border px-3 py-2 text-left transition-colors peer-focus-visible:outline-2 peer-focus-visible:outline-offset-2 peer-focus-visible:outline-focus-ring peer-disabled:cursor-not-allowed peer-disabled:opacity-50",
                        selected
                          ? "border-accent-emphasis bg-accent-subtle text-fg"
                          : "border-border bg-surface text-fg peer-hover:bg-surface-hover"
                      )}
                    >
                      <span className="flex items-center justify-between gap-2">
                        <span className="text-sm font-semibold">{profileLabel(item.name)}</span>
                        {selected ? (
                          <CheckCircle2 size={16} className="shrink-0 text-accent-fg" aria-hidden />
                        ) : null}
                      </span>
                      <span className="mt-1 block text-xs leading-relaxed text-fg-muted">
                        {profileDescription(item.name)}
                      </span>
                      <ProfileChips profile={item} />
                    </label>
                  </div>
                );
              })}
            </div>
          </div>
          <dl className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-4">
            <RuntimeFact
              label={t("settings.vectorIndex.targetAccuracy")}
              value={String(settings.target_accuracy)}
            />
            <RuntimeFact
              label={t("settings.vectorIndex.build")}
              value={`${t("settings.vectorIndex.neighbors")} ${settings.neighbors} / ${t(
                "settings.vectorIndex.efconstruction"
              )} ${settings.efconstruction}`}
            />
            <RuntimeFact label={t("settings.vectorIndex.distance")} value={settings.distance} />
            <RuntimeFact
              label={t("settings.vectorIndex.currentIndex")}
              value={currentIndexLabel(settings)}
            />
          </dl>
          {reprovision ? <FormStatus tone={reprovision.tone} message={t(reprovision.key)} /> : null}
          <FormActionBar
            ariaLabel={t("settings.vectorIndex.actions.label")}
            primaryActions={[
              {
                id: "save",
                label: t("settings.vectorIndex.actions.save"),
                icon: Save,
                loading: save.isPending,
                disabled: !dirty,
                onClick: submit,
              },
            ]}
            secondaryActions={[
              {
                id: "reset",
                label: t("settings.vectorIndex.actions.reset"),
                icon: RotateCcw,
                disabled: !dirty || save.isPending,
                onClick: resetForm,
              },
            ]}
            status={
              save.isError ? (
                <FormStatus tone="danger" message={saveError} />
              ) : dirty ? (
                <FormStatus tone="warning" message={t("settings.vectorIndex.actions.unsaved")} />
              ) : null
            }
          />
        </CardContent>
      </Card>
      {showReindexSql(settings) ? (
        <SettingsPreviewCard
          icon={Database}
          title={t("settings.vectorIndex.reindexSql.title")}
          description={t("settings.vectorIndex.reindexSql.description")}
          value={settings.reindex_sql}
          copyLabel={t("settings.vectorIndex.reindexSql.copy")}
          previewRows={7}
        />
      ) : null}
    </PageBody>
  );
}

function ProfileChips({ profile }: { profile: VectorIndexProfileStatusData }) {
  return (
    <span className="mt-2 flex flex-wrap gap-1">
      <span data-surface-tint="info" className="inline-flex min-h-5 items-center rounded bg-info-subtle px-1.5 text-xs font-medium text-info-fg">
        {t("settings.vectorIndex.targetAccuracy")} {profile.target_accuracy}
      </span>
      <span data-surface-tint="neutral" className="inline-flex min-h-5 items-center rounded bg-surface-hover px-1.5 text-xs text-fg-muted">
        {t("settings.vectorIndex.neighbors")} {profile.neighbors}
      </span>
      <span data-surface-tint="neutral" className="inline-flex min-h-5 items-center rounded bg-surface-hover px-1.5 text-xs text-fg-muted">
        {t("settings.vectorIndex.efconstruction")} {profile.efconstruction}
      </span>
    </span>
  );
}

function RuntimeFact({ label, value }: { label: string; value: string }) {
  return (
    <div data-surface-tint="neutral" className="rounded-md border border-border bg-surface-hover p-3">
      <dt className="text-xs font-medium text-fg-muted">{label}</dt>
      <dd className="mt-1 break-words text-sm font-semibold text-fg">{value}</dd>
    </div>
  );
}

export function orderedProfiles(
  profiles: VectorIndexProfileStatusData[]
): VectorIndexProfileStatusData[] {
  const byName = new Map(profiles.map((item) => [item.name, item]));
  const ordered = PROFILE_ORDER.map((name) => byName.get(name)).filter(
    (item): item is VectorIndexProfileStatusData => Boolean(item)
  );
  return ordered.length ? ordered : profiles;
}

/** 選択中の検索精度の判定から、出す案内を決める。一致(match)なら何も出さない。 */
export function reprovisionStatus(
  status: VectorIndexBuildStatus | undefined
): { tone: "warning" | "info"; key: I18nKey } | null {
  if (status === "reprovision") return { tone: "warning", key: "settings.vectorIndex.reprovision" };
  if (status === "unknown") return { tone: "info", key: "settings.vectorIndex.reprovisionUnknown" };
  return null;
}

/** 保存済みの検索精度が実際の索引と一致するときは、再作成 SQL を出さない。 */
export function showReindexSql(settings: VectorIndexSettingsData) {
  return settings.index_status !== "match" && Boolean(settings.reindex_sql);
}

/** 現在の索引の値。確認できないときはその旨を出す。 */
export function currentIndexLabel(settings: VectorIndexSettingsData) {
  if (settings.actual_neighbors == null || settings.actual_efconstruction == null) {
    return t("settings.vectorIndex.currentIndex.unknown");
  }
  return `${t("settings.vectorIndex.neighbors")} ${settings.actual_neighbors} / ${t(
    "settings.vectorIndex.efconstruction"
  )} ${settings.actual_efconstruction}`;
}

function profileLabel(name: VectorIndexProfileName) {
  return t(`settings.vectorIndex.profile.${name}` as I18nKey);
}

function profileDescription(name: VectorIndexProfileName) {
  return t(`settings.vectorIndex.profile.${name}.description` as I18nKey);
}
