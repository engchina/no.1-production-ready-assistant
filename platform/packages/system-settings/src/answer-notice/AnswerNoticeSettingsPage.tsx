import {
  AnswerNotice,
  Banner,
  Button,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  ChatAnswer,
  ErrorState,
  FormActionBar,
  FormSkeleton,
  FormStatus,
  PageBody,
  Switch,
  TextField,
  TextareaField,
  TimedLoadingState,
  toast,
} from "@production-ready/ui";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Eye, MessageSquareWarning, RefreshCw, RotateCcw, Save } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";

import { formatMessage } from "../auth/messages";
import { useSettingsDraftGuard, type DraftGuardMessages } from "../guards/useSettingsDraftGuard";
import { ANSWER_NOTICE_MESSAGES, type AnswerNoticeMessages } from "./messages";
import {
  ANSWER_NOTICE_QUERY_KEY,
  type AnswerNoticeApi,
  type AnswerNoticeSettingsData,
  type AnswerNoticeSettingsUpdate,
} from "./types";

interface AnswerNoticeForm {
  enabled: boolean;
  text: string;
  contact: string;
}

const EMPTY_FORM: AnswerNoticeForm = { enabled: true, text: "", contact: "" };
/** 窓口の案内の 1 行の既定の雛形（古い backend の応答に無いとき。backend の `ANSWER_NOTICE_CONTACT_TEMPLATE`）。 */
const DEFAULT_CONTACT_TEMPLATE = "解決しない場合は、{contact}へお問い合わせください。";

export interface AnswerNoticeSettingsPageProps {
  /** 製品の API 関数（GET / PUT /api/settings/answer-notice）。 */
  api: AnswerNoticeApi;
  /** API エラーから画面に出すメッセージを取り出す（製品の ApiError など）。undefined なら既定の文言。 */
  errorMessage?: (error: unknown) => string | undefined;
  /** 既定の文言の上書き。 */
  messages?: Partial<AnswerNoticeMessages>;
  /** 離脱確認ダイアログの文言の上書き。 */
  draftGuardMessages?: Partial<DraftGuardMessages>;
}

/** 改行をそろえ、行末の空白と前後の空行を除く（backend の `normalize_notice_text` と同じ規則）。 */
export function normalizeAnswerNoticeText(value: string): string {
  return value
    .replace(/\r\n?/g, "\n")
    .split("\n")
    .map((line) => line.replace(/\s+$/, ""))
    .join("\n")
    .trim();
}

/** 窓口は 1 行（改行・連続の空白を 1 つの空白にそろえる。backend の `normalize_notice_contact` と同じ）。 */
export function normalizeAnswerNoticeContact(value: string): string {
  return value.split(/\s+/).filter(Boolean).join(" ");
}

/** 本文の後に窓口の案内の 1 行を続けた、回答に付ける文（backend の `compose_answer_notice` と同じ）。 */
export function composeAnswerNotice(body: string, contact: string, template = DEFAULT_CONTACT_TEMPLATE): string {
  const line = contact ? template.replace("{contact}", contact) : "";
  return [body, line].filter(Boolean).join("\n");
}

/** 保存する本文と窓口。既定と同じなら null（既定は DB に書き込まない）。 */
export function answerNoticePayload(
  form: AnswerNoticeForm,
  defaultText: string,
  defaultContact = "",
): AnswerNoticeSettingsUpdate {
  const text = normalizeAnswerNoticeText(form.text);
  const contact = normalizeAnswerNoticeContact(form.contact);
  return {
    enabled: form.enabled,
    text: text === defaultText ? null : text,
    contact: !contact || contact === defaultContact ? null : contact,
  };
}

function formFromSettings(data: AnswerNoticeSettingsData): AnswerNoticeForm {
  return {
    enabled: data.enabled,
    text: data.text ?? data.default_text,
    contact: data.contact ?? data.default_contact ?? "",
  };
}

/**
 * 回答の注意書きの設定（画面は 3 製品で共通。#2405）。
 *
 * - 設定（出す / 出さない・本文・窓口）は製品ごと（共通の表の製品の行。#2449）。保存したら次の回答から効く。
 * - 「既定の文に戻す」は入力欄を既定の文にする（保存すると既定に戻り、既定の文は DB に書き込まない）。
 * - 下の「表示の確認」に、回答の末尾に出す見た目（`AnswerNotice`）を入力中の値で出す。
 */
export function AnswerNoticeSettingsPage({
  api,
  errorMessage,
  messages,
  draftGuardMessages,
}: AnswerNoticeSettingsPageProps) {
  const m = { ...ANSWER_NOTICE_MESSAGES, ...messages };
  const enabledLabelId = useId();
  const enabledHintId = useId();
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: ANSWER_NOTICE_QUERY_KEY,
    queryFn: ({ signal }) => api.get({ signal }),
  });
  const save = useMutation({
    mutationFn: (payload: AnswerNoticeSettingsUpdate) => api.update(payload),
    onSuccess: (data) => {
      queryClient.setQueryData(ANSWER_NOTICE_QUERY_KEY, data);
    },
  });
  const [form, setForm] = useState<AnswerNoticeForm>(EMPTY_FORM);
  const [error, setError] = useState<string | undefined>();
  const [contactError, setContactError] = useState<string | undefined>();
  const baseline = useRef(EMPTY_FORM);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  useSettingsDraftGuard(
    form.enabled !== baseline.current.enabled ||
      normalizeAnswerNoticeText(form.text) !== normalizeAnswerNoticeText(baseline.current.text) ||
      normalizeAnswerNoticeContact(form.contact) !== normalizeAnswerNoticeContact(baseline.current.contact),
    save.isPending,
    draftGuardMessages,
  );

  useEffect(() => {
    if (!query.data) return;
    const next = formFromSettings(query.data);
    const previous = baseline.current;
    baseline.current = next;
    // 背景の再取得では、利用者が編集していない項目だけを更新する。
    setForm((current) => ({
      enabled: current.enabled === previous.enabled ? next.enabled : current.enabled,
      text: current.text === previous.text ? next.text : current.text,
      contact: current.contact === previous.contact ? next.contact : current.contact,
    }));
  }, [query.data]);

  function updateForm(update: Partial<AnswerNoticeForm>) {
    if (save.isPending) return;
    setForm((current) => ({ ...current, ...update }));
    if ("text" in update) setError(undefined);
    if ("contact" in update) setContactError(undefined);
    save.reset();
  }

  function submit() {
    if (save.isPending || !query.data) return;
    const text = normalizeAnswerNoticeText(form.text);
    const validation = !text
      ? m.validationRequired
      : text.length > query.data.max_chars
        ? formatMessage(m.validationTooLong, { max: query.data.max_chars })
        : undefined;
    const contactMax = query.data.contact_max_chars ?? 200;
    const contactValidation =
      normalizeAnswerNoticeContact(form.contact).length > contactMax
        ? formatMessage(m.validationContactTooLong, { max: contactMax })
        : undefined;
    if (validation || contactValidation) {
      setError(validation);
      setContactError(contactValidation);
      if (validation) textareaRef.current?.focus();
      else document.getElementById("answer-notice-contact")?.focus();
      return;
    }
    setError(undefined);
    setContactError(undefined);
    save.mutate(answerNoticePayload(form, query.data.default_text, query.data.default_contact ?? ""), {
      onSuccess: (data) => {
        baseline.current = formFromSettings(data);
        setForm(baseline.current);
        toast.success(m.saved);
      },
    });
  }

  if (query.isPending) {
    return (
      <PageBody wide>
        <TimedLoadingState label={m.loading} operationKey="answer-notice-settings" testId="settings-answer-notice-loading">
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

  const data = query.data;
  const normalizedText = normalizeAnswerNoticeText(form.text);
  const normalizedContact = normalizeAnswerNoticeContact(form.contact);
  const defaultContact = data.default_contact ?? "";
  const contactTemplate = data.contact_template ?? DEFAULT_CONTACT_TEMPLATE;
  const isDefaultText =
    normalizedText === data.default_text && (normalizedContact || defaultContact) === defaultContact;
  const previewText = composeAnswerNotice(normalizedText, normalizedContact || defaultContact, contactTemplate);
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
      <div className="space-y-5">
        <form
          noValidate
          onSubmit={(event) => {
            event.preventDefault();
            submit();
          }}
        >
          <fieldset disabled={save.isPending} aria-busy={save.isPending} className="min-w-0">
            <Card>
              <CardHeader>
                <CardTitle className="flex items-center gap-2">
                  <MessageSquareWarning size={16} className="text-accent-fg" aria-hidden />
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
                    checked={form.enabled}
                    aria-labelledby={enabledLabelId}
                    aria-describedby={enabledHintId}
                    onCheckedChange={(enabled) => updateForm({ enabled })}
                    data-testid="answer-notice-enabled"
                  />
                </div>
                <TextareaField
                  ref={textareaRef}
                  id="answer-notice-text"
                  label={m.fieldText}
                  value={form.text}
                  onValueChange={(text) => updateForm({ text })}
                  helper={
                    <>
                      {m.helperText}
                      <span className="mt-1 block" data-testid="answer-notice-source">
                        {isDefaultText ? m.usingDefault : m.usingCustom}
                      </span>
                    </>
                  }
                  error={error}
                  rows={4}
                  maxLength={data.max_chars}
                  showCount
                  width="full"
                  required
                />
                <TextField
                  id="answer-notice-contact"
                  label={m.fieldContact}
                  value={form.contact}
                  onValueChange={(contact) => updateForm({ contact })}
                  helper={formatMessage(m.helperContact, {
                    line: contactTemplate.replace("{contact}", `〈${m.fieldContact}〉`),
                  })}
                  placeholder={defaultContact}
                  error={contactError}
                  maxLength={data.contact_max_chars ?? 200}
                  width="full"
                />
                <FormActionBar
                  ariaLabel={m.actionsLabel}
                  primaryActions={[{ id: "save", type: "submit", label: m.save, icon: Save, loading: save.isPending }]}
                  secondaryActions={[
                    {
                      id: "reset",
                      label: m.resetToDefault,
                      icon: RotateCcw,
                      disabled: isDefaultText || save.isPending,
                      onClick: () => updateForm({ text: data.default_text, contact: defaultContact }),
                    },
                  ]}
                  status={save.isError ? <FormStatus tone="danger" message={saveError} /> : null}
                />
              </CardContent>
            </Card>
          </fieldset>
        </form>
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Eye size={16} className="text-accent-fg" aria-hidden />
              {m.previewTitle}
            </CardTitle>
            <CardDescription>{m.previewDescription}</CardDescription>
          </CardHeader>
          <CardContent>
            <ChatAnswer testId="answer-notice-preview">
              <p className="text-sm text-fg-muted">{m.previewAnswer}</p>
              {form.enabled ? (
                <AnswerNotice notice={previewText} label={m.noticeLabel} testId="answer-notice-preview-notice" />
              ) : (
                <p className="text-xs text-fg-muted">{m.previewDisabled}</p>
              )}
            </ChatAnswer>
          </CardContent>
        </Card>
      </div>
    </PageBody>
  );
}
