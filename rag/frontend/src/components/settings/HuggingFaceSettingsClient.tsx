"use client";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  FormActionBar,
  FormGrid,
  FormStatus,
  PageBody,
  SecretField,
  Skeleton,
  TextField,
  TimedLoadingState,
} from "@production-ready/ui";
import { HardDriveDownload, Save } from "lucide-react";
import { useState } from "react";

import { ApiErrorState } from "@/components/StateViews";
import {
  ApiError,
  type HuggingFaceSettingsData,
  type HuggingFaceSettingsUpdate,
} from "@/lib/api";
import { t } from "@/lib/i18n";
import { useLeaveGuard } from "@/lib/leave-guard";
import { useValuesChanged } from "@/lib/render-sync";
import { useHuggingFaceSettings, useUpdateHuggingFaceSettings } from "@/lib/queries";
import { toast } from "@/lib/toast";

interface HuggingFaceForm {
  endpoint: string;
  token: string;
  clearToken: boolean;
}

const EMPTY_FORM: HuggingFaceForm = {
  endpoint: "",
  token: "",
  clearToken: false,
};

/**
 * HuggingFace モデルダウンロード（token / ミラー）の設定（#287）。
 *
 * 共通のシステム設定の画面（`@production-ready/system-settings` のモデル設定・アップロード保存先）と
 * 同じ構成にそろえる。
 * - 読み込み中は TimedLoadingState + Skeleton、取得の失敗は ErrorState（再試行）
 * - 保存中は入力と再送信を止め、成功は Toast、失敗は操作行の FormStatus に出す
 * - 背景の再取得では、利用者が編集していない項目だけを更新する（UX 契約 workspace-state.md）
 * - 未保存の変更と保存中の離脱は共通の離脱ガードで確認する
 */
export function HuggingFaceSettingsClient() {
  const query = useHuggingFaceSettings();
  const save = useUpdateHuggingFaceSettings();

  const [server, setServer] = useState<HuggingFaceSettingsData | null>(null);
  const [form, setForm] = useState<HuggingFaceForm>(EMPTY_FORM);
  const [tokenVisible, setTokenVisible] = useState(false);

  // server 値が変わったレンダーで、編集していない項目だけを server 値に合わせる（effect で setState しない）。
  const serverChanged = useValuesChanged([query.data]);
  if (serverChanged && query.data) {
    const next = formFromSettings(query.data);
    const merged = server ? keepEditedFields(form, formFromSettings(server), next) : next;
    // 保存済み token がなくなったら削除の指定を外す（token 欄が無効のまま残らないように）。
    setForm(query.data.token_configured ? merged : { ...merged, clearToken: false });
    setServer(query.data);
  }
  const settings = server ?? query.data ?? null;

  const isDirty = Boolean(
    settings && (form.endpoint !== settings.endpoint || form.token !== "" || form.clearToken)
  );
  // token は保存しない。入力中の endpoint / token / 削除の指定があるときと保存中は離脱を確認する。
  useLeaveGuard(isDirty, save.isPending);

  function updateForm(update: Partial<HuggingFaceForm>) {
    if (save.isPending) return;
    setForm((current) => ({ ...current, ...update }));
    save.reset();
  }

  function submit() {
    if (save.isPending || !settings) return;
    save.mutate(payloadFromForm(form), {
      onSuccess: (data) => {
        setServer(data);
        setForm(formFromSettings(data));
        setTokenVisible(false);
        toast.success(t("settings.huggingface.actions.saved"));
      },
    });
  }

  if (!settings) {
    if (query.isError) {
      return (
        <PageBody wide>
          <ApiErrorState
            error={query.error}
            fallback={t("settings.huggingface.loadError")}
            onRetry={() => void query.refetch()}
          />
        </PageBody>
      );
    }
    return (
      <PageBody wide>
        <TimedLoadingState
          label={t("settings.huggingface.loading")}
          operationKey="settings-huggingface-load"
          placement="page"
          testId="settings-huggingface-loading"
        >
          {/* 設定カード（見出し・入力 2 項目・操作行・補足）の寸法を予約する。 */}
          <Skeleton className="h-96 w-full rounded-lg" />
        </TimedLoadingState>
      </PageBody>
    );
  }

  const saveError =
    save.error instanceof ApiError ? save.error.message : t("settings.huggingface.saveError");

  return (
    <PageBody wide>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          submit();
        }}
      >
        <fieldset
          disabled={save.isPending}
          aria-busy={save.isPending}
          className="min-w-0 space-y-6"
        >
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <HardDriveDownload size={16} className="text-accent-fg" aria-hidden />
                {t("settings.huggingface.cardTitle")}
              </CardTitle>
              <CardDescription>{t("settings.huggingface.cardDescription")}</CardDescription>
            </CardHeader>
            <CardContent className="space-y-5">
              {/* ミラー endpoint は URL なので全幅（full）、token は長い値の段（lg）で次の行に置く（#1891）。 */}
              <FormGrid>
                <TextField
                  id="hf-endpoint"
                  className="col-span-full"
                  width="full"
                  label={t("settings.huggingface.field.endpoint")}
                  value={form.endpoint}
                  onValueChange={(value) => updateForm({ endpoint: value })}
                  placeholder={t("settings.huggingface.placeholder.endpoint")}
                  helper={t("settings.huggingface.helper.endpoint")}
                  autoComplete="off"
                />
                <SecretField
                  id="hf-token"
                  width="lg"
                  label={t("settings.huggingface.field.token")}
                  value={form.token}
                  onValueChange={(value) => updateForm({ token: value })}
                  visible={tokenVisible}
                  onVisibleChange={setTokenVisible}
                  hasSavedSecret={settings.token_configured}
                  savedLabel={t("settings.huggingface.secrets.saved")}
                  notSetLabel={t("settings.huggingface.secrets.notSet")}
                  showLabel={t("settings.huggingface.secrets.show")}
                  hideLabel={t("settings.huggingface.secrets.hide")}
                  placeholder={
                    settings.token_configured
                      ? t("settings.huggingface.placeholder.tokenSaved")
                      : t("settings.huggingface.placeholder.token")
                  }
                  helper={
                    settings.token_configured
                      ? t("settings.huggingface.helper.tokenSaved")
                      : t("settings.huggingface.helper.token")
                  }
                  clearOption={{
                    label: t("settings.huggingface.secrets.clearToken"),
                    checked: form.clearToken,
                    onCheckedChange: (checked) =>
                      updateForm({ clearToken: checked, token: checked ? "" : form.token }),
                  }}
                />
              </FormGrid>

              <FormActionBar
                ariaLabel={t("settings.huggingface.actions.label")}
                primaryActions={[
                  {
                    id: "save",
                    label: t("settings.huggingface.actions.save"),
                    icon: Save,
                    loading: save.isPending,
                    testId: "settings-huggingface-save",
                    type: "submit",
                  },
                ]}
                status={save.isError ? <FormStatus tone="danger" message={saveError} /> : null}
              />

              <div className="space-y-1 text-xs leading-relaxed text-fg-muted">
                <p>{t("settings.huggingface.hint")}</p>
                <p>{t("settings.huggingface.hintCache")}</p>
              </div>
            </CardContent>
          </Card>
        </fieldset>
      </form>
    </PageBody>
  );
}

function formFromSettings(settings: HuggingFaceSettingsData): HuggingFaceForm {
  return {
    endpoint: settings.endpoint,
    token: "",
    clearToken: false,
  };
}

/** 前回の server 値から変えていない項目だけを新しい server 値にする。 */
function keepEditedFields(
  current: HuggingFaceForm,
  previous: HuggingFaceForm,
  next: HuggingFaceForm
): HuggingFaceForm {
  return {
    endpoint: current.endpoint === previous.endpoint ? next.endpoint : current.endpoint,
    token: current.token === previous.token ? next.token : current.token,
    clearToken: current.clearToken === previous.clearToken ? next.clearToken : current.clearToken,
  };
}

function payloadFromForm(form: HuggingFaceForm): HuggingFaceSettingsUpdate {
  const payload: HuggingFaceSettingsUpdate = {
    endpoint: form.endpoint,
  };
  if (form.clearToken) payload.clear_token = true;
  else if (form.token !== "") payload.token = form.token;
  return payload;
}
