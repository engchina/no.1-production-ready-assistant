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
  FormGrid,
  FormSkeleton,
  FormStatus,
  PageBody,
  TextField,
  TimedLoadingState,
  toast,
} from "@production-ready/ui";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { HardDrive, RefreshCw, Save } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { useSettingsDraftGuard, type DraftGuardMessages } from "../guards/useSettingsDraftGuard";
import { UPLOAD_STORAGE_MESSAGES, type UploadStorageMessages } from "./messages";
import {
  UPLOAD_STORAGE_QUERY_KEY,
  type UploadStorageApi,
  type UploadStorageSettingsData,
  type UploadStorageSettingsUpdate,
} from "./types";

interface UploadStorageForm {
  localStorageDir: string;
}

type FieldErrors = Partial<Record<keyof UploadStorageForm, string>>;

const EMPTY_FORM: UploadStorageForm = { localStorageDir: "" };

export interface UploadStorageSettingsPageProps {
  /** 製品の API 関数（GET / PATCH /api/settings/upload-storage）。 */
  api: UploadStorageApi;
  /** 入力欄の placeholder（製品ごとの既定の保存先）。 */
  placeholders?: { localStorageDir?: string };
  /** API エラーから画面に出すメッセージを取り出す（製品の ApiError など）。undefined なら既定の文言。 */
  errorMessage?: (error: unknown) => string | undefined;
  /** 既定の文言の上書き。 */
  messages?: Partial<UploadStorageMessages>;
  /** 離脱確認ダイアログの文言の上書き。 */
  draftGuardMessages?: Partial<DraftGuardMessages>;
}

/**
 * アップロード原本の保存先設定（3製品共通。NL2SQL の画面を基準に移設。#97）。
 *
 * 保存先はローカルのディレクトリだけ（OCI Object Storage は扱わない。#11）。
 * - 背景の再取得では、利用者が編集していない項目だけを更新する（NL2SQL #378）
 * - 保存中は入力と再送信を止める
 */
export function UploadStorageSettingsPage({
  api,
  placeholders,
  errorMessage,
  messages,
  draftGuardMessages,
}: UploadStorageSettingsPageProps) {
  const m = { ...UPLOAD_STORAGE_MESSAGES, ...messages };
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: UPLOAD_STORAGE_QUERY_KEY,
    queryFn: ({ signal }) => api.get({ signal }),
  });
  const save = useMutation({
    mutationFn: (payload: UploadStorageSettingsUpdate) => api.update(payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: UPLOAD_STORAGE_QUERY_KEY });
    },
  });
  const [form, setForm] = useState<UploadStorageForm>(EMPTY_FORM);
  const [errors, setErrors] = useState<FieldErrors>({});

  const baseline = useRef(EMPTY_FORM);
  useSettingsDraftGuard(
    JSON.stringify(form) !== JSON.stringify(baseline.current),
    save.isPending,
    draftGuardMessages,
  );

  useEffect(() => {
    if (!query.data) return;
    const next = formFromSettings(query.data);
    const previous = baseline.current;
    baseline.current = next;
    setForm((current) => ({
      localStorageDir:
        current.localStorageDir === previous.localStorageDir
          ? next.localStorageDir
          : current.localStorageDir,
    }));
  }, [query.data]);

  function updateForm(update: Partial<UploadStorageForm>) {
    if (save.isPending) return;
    setForm((current) => ({ ...current, ...update }));
    setErrors((current) => {
      const next = { ...current };
      for (const key of Object.keys(update) as Array<keyof UploadStorageForm>) {
        delete next[key];
      }
      return next;
    });
    save.reset();
  }

  function submit() {
    if (save.isPending || !query.data) return;
    const validationErrors = validateUploadStorageForm(form, m);
    if (Object.keys(validationErrors).length > 0) {
      setErrors(validationErrors);
      save.reset();
      // エラーの表示（aria-describedby）が描画された後に移す（#531）。
      window.setTimeout(() => document.getElementById("upload-storage-local-dir")?.focus(), 0);
      return;
    }

    setErrors({});
    save.mutate(payloadFromForm(form), {
      onSuccess: (data) => {
        baseline.current = formFromSettings(data);
        setForm(baseline.current);
        setErrors({});
        toast.success(m.saved);
      },
    });
  }

  if (query.isPending) {
    // 読み込み中は経過時間と、保存先のカードの形の Skeleton を出す（AGENTS.md「読み込み中」。#1047）。
    return (
      <PageBody wide>
        <TimedLoadingState
          label={m.loading}
          operationKey="upload-storage-settings"
          testId="settings-upload-storage-loading"
        >
          <FormSkeleton fields={1} />
        </TimedLoadingState>
      </PageBody>
    );
  }

  // 初回の取得に失敗したときだけ再試行を出して編集させない。取得済みの値がある再取得の失敗
  // （保存後・画面に戻ったときの再取得）では、入力中のフォームを隠さず、上に警告を出す（#1047）。
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
        className="space-y-5"
        onSubmit={(event) => {
          event.preventDefault();
          submit();
        }}
      >
        <fieldset
          disabled={save.isPending}
          aria-busy={save.isPending}
          className="min-w-0 space-y-5"
        >
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <HardDrive size={16} className="text-accent-fg" aria-hidden />
                {m.destinationTitle}
              </CardTitle>
              <CardDescription>{m.destinationDescription}</CardDescription>
            </CardHeader>
            <CardContent className="space-y-5">
              {/* パスは全幅（FormGrid の最大 60rem まで。#1891）。 */}
              <FormGrid columns={1}>
                <TextField
                  id="upload-storage-local-dir"
                  label={m.fieldLocalStorageDir}
                  value={form.localStorageDir}
                  onValueChange={(value) => updateForm({ localStorageDir: value })}
                  helper={m.helperLocalStorageDir}
                  placeholder={placeholders?.localStorageDir}
                  error={errors.localStorageDir}
                  width="full"
                  required
                  requiredLabel={m.required}
                />
              </FormGrid>

              <FormActionBar
                ariaLabel={m.actionsLabel}
                primaryActions={[
                  {
                    id: "save",
                    type: "submit",
                    label: m.save,
                    icon: Save,
                    loading: save.isPending,
                  },
                ]}
                status={save.isError ? <FormStatus tone="danger" message={saveError} /> : null}
              />
            </CardContent>
          </Card>
        </fieldset>
      </form>
    </PageBody>
  );
}

function formFromSettings(settings: UploadStorageSettingsData): UploadStorageForm {
  return { localStorageDir: settings.local_storage_dir };
}

function payloadFromForm(form: UploadStorageForm): UploadStorageSettingsUpdate {
  return { local_storage_dir: form.localStorageDir };
}

/** 保存前の検証（backend の pr_system_settings と同じ規則）。 */
export function validateUploadStorageForm(
  form: UploadStorageForm,
  m: UploadStorageMessages = UPLOAD_STORAGE_MESSAGES,
): FieldErrors {
  const errors: FieldErrors = {};
  if (!form.localStorageDir.trim()) {
    errors.localStorageDir = m.validationLocalStorageDir;
  }
  return errors;
}
