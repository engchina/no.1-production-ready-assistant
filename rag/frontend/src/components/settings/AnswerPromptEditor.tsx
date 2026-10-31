import {
  Disclosure,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  FormActionBar,
  FormGrid,
  FormStatus,
  Skeleton,
  StatusBadge,
  TextareaField,
  TimedLoadingState,
} from "@production-ready/ui";
import { History, RotateCcw, Save } from "lucide-react";
import { useState } from "react";

import { ErrorState } from "@/components/StateViews";
import { ApiError, type AnswerPromptKey, type AnswerPromptView } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { t, type I18nKey } from "@/lib/i18n";
import { useLeaveGuard } from "@/lib/leave-guard";
import { useAnswerPrompts, useSaveAnswerPrompt } from "@/lib/queries";
import { focusFirstInvalidField } from "@/lib/required-fields";
import { toast } from "@/lib/toast";

import { answerPromptError } from "./answer-prompt.logic";

const PROMPT_MAX = 50000;

/**
 * 編集できるプロンプト（回答生成のプロンプト `vlm_answer` / 図・画像の読み取りプロンプト `image_retrieval`）を 1 つ編集する。
 * 全体で 1 つの設定（検索・回答プロファイル・文書レシピでは上書きしない）。保存した内容は次の回答・次の解析から使う。
 */
export function AnswerPromptCard({ promptKey }: { promptKey: AnswerPromptKey }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>{t(`settings.answerPrompts.${promptKey}.title` as I18nKey)}</CardTitle>
        <CardDescription>{t(`settings.answerPrompts.${promptKey}.description` as I18nKey)}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <AnswerPromptPanel promptKey={promptKey} />
      </CardContent>
    </Card>
  );
}

/**
 * カードの枠を持たないプロンプトの編集欄。ほかの設定の節の中に置くときに使う
 * （文書解析の「解析後の処理」の Vision の中の読み取りプロンプト。#528）。
 */
export function AnswerPromptPanel({ promptKey, fieldLabel }: { promptKey: AnswerPromptKey; fieldLabel?: string }) {
  const query = useAnswerPrompts();
  const prompt = query.data?.prompts.find((item) => item.key === promptKey);
  if (query.isPending) {
    // ほかの読み込み中と同じく、先頭に文言と経過時間の行、その下に内容の形の Skeleton（#2510）。
    return (
      <TimedLoadingState
        label={t("settings.answerPrompts.loading")}
        operationKey={`answer-prompt-load:${promptKey}`}
        framed={false}
        testId="answer-prompt-loading"
      >
        <Skeleton className="h-40 w-full" />
      </TimedLoadingState>
    );
  }
  if (query.isError || !prompt) {
    return (
      <ErrorState message={t("settings.answerPrompts.loadError")} onRetry={() => void query.refetch()} />
    );
  }
  return (
    <div className="space-y-4">
      <AnswerPromptEditor prompt={prompt} fieldLabel={fieldLabel} />
    </div>
  );
}

function AnswerPromptEditor({ prompt, fieldLabel: suppliedLabel }: { prompt: AnswerPromptView; fieldLabel?: string }) {
  const save = useSaveAnswerPrompt();
  const [content, setContent] = useState(prompt.content);
  // 編集欄が基にした保存値。保存値が変わったレンダーで、未編集なら編集欄を保存値へ揃える。
  // 編集中の内容は背景の再取得で上書きしない(UX 契約 workspace-state。#276)。
  const [baseContent, setBaseContent] = useState(prompt.content);
  if (prompt.content !== baseContent) {
    setBaseContent(prompt.content);
    if (content === baseContent) setContent(prompt.content);
  }
  const [restoreDefault, setRestoreDefault] = useState(false);
  const dirty = content !== prompt.content || (restoreDefault && prompt.customized);
  // 保存を押した後だけ欄のエラーを出す（直すと消える）。
  const [showError, setShowError] = useState(false);
  const fieldLabel = suppliedLabel ?? t(`settings.answerPrompts.${prompt.key}.field` as I18nKey);
  const error = answerPromptError(content, fieldLabel, prompt.required_placeholders, prompt.required_tokens);
  // 保存中は離脱を止める。
  useLeaveGuard(dirty, save.isPending);
  const inputId = `answer-prompt-${prompt.key}`;

  function submit() {
    if (save.isPending) return;
    // 空・必須の placeholder の欠けは送る前に欄の下へ出す（backend の validate_prompt と同じ規則。#1010）。
    setShowError(true);
    if (focusFirstInvalidField([[inputId, error]])) return;
    save.mutate(
      { key: prompt.key, content: restoreDefault ? null : content },
      // 保存の成功は Toast、失敗は操作の行の FormStatus（messaging.md §10.2）。
      { onSuccess: () => {
          setRestoreDefault(false);
          toast.success(t("settings.answerPrompts.saved"));
        } }
    );
  }

  function discard() {
    save.reset();
    setShowError(false);
    setContent(prompt.content);
    setRestoreDefault(false);
  }

  function reset() {
    save.reset();
    setShowError(false);
    setContent(prompt.default_content);
    setRestoreDefault(true);
  }

  return (
    <div className="space-y-2">
      {/* 空のままでは保存できない（既定へ戻すのは「既定に戻す」）ので必須（#531）。 */}
      {/* プロンプトの編集欄はフォームの最大幅まで（1920px で 1 行が長くなりすぎない。#1891）。 */}
      <FormGrid columns={1}>
        <TextareaField
          id={inputId}
          width="full"
          label={fieldLabel}
          required
          helper={prompt.required_placeholders.length || prompt.required_tokens?.length ? t("settings.answerPrompts.placeholders", {
            names: [...prompt.required_placeholders.map((name) => `{{${name}}}`), ...(prompt.required_tokens ?? [])].join("、"),
          }) : t("settings.answerPrompts.noPlaceholders")}
          error={showError ? (error ?? undefined) : undefined}
          value={content}
          maxLength={PROMPT_MAX}
          rows={16}
          spellCheck={false}
          monospace
          disabled={save.isPending}
          onChange={(event) => {
            save.reset();
            setContent(event.target.value);
            if (event.target.value !== prompt.default_content) setRestoreDefault(false);
          }}
        />
      </FormGrid>
      <FormActionBar
        ariaLabel={t("settings.answerPrompts.actions.label")}
        primaryActions={[
          {
            id: "save",
            label: t("settings.answerPrompts.save"),
            icon: Save,
            loading: save.isPending,
            disabled: !dirty || save.isPending,
            onClick: submit,
          },
        ]}
        secondaryActions={[
          {
            id: "discard",
            label: t("settings.answerPrompts.discard"),
            icon: RotateCcw,
            disabled: !dirty || save.isPending,
            onClick: discard,
          },
          {
            id: "restore-default",
            label: t("settings.answerPrompts.reset"),
            icon: History,
            disabled: save.isPending || (content === prompt.default_content && (!prompt.customized || restoreDefault)),
            onClick: reset,
          },
        ]}
        status={
          save.isError ? (
            <FormStatus
              tone="danger"
              message={
                save.error instanceof ApiError ? save.error.message : t("settings.answerPrompts.saveError")
              }
            />
          ) : dirty ? (
            <FormStatus tone="warning" message={t(restoreDefault ? "settings.answerPrompts.resetPending" : "settings.answerPrompts.unsaved")} />
          ) : (
            prompt.customized ? <StatusBadge
              variant="info"
              label={t("settings.answerPrompts.customized", {
                value: prompt.updated_at ? formatDateTime(prompt.updated_at) : "—",
              })}
            /> : null
          )
        }
      />
    </div>
  );
}

/** 各機能の設定で共通のプロンプト編集操作を示す。 */
export function FeaturePromptDisclosure({ promptKey }: { promptKey: AnswerPromptKey }) {
  return (
    <Disclosure
      summary={t(`settings.answerPrompts.${promptKey}.title` as I18nKey)}
      description={t(`settings.answerPrompts.${promptKey}.description` as I18nKey)}
      surface="sunken"
    >
      <AnswerPromptPanel promptKey={promptKey} />
      {promptKey === "image_retrieval" ? <VisionPromptRules /> : null}
    </Disclosure>
  );
}


/** 画像の編集可能な指示と、固定の出力契約を同じ場所で確認する。 */
function VisionPromptRules() {
  const query = useAnswerPrompts();
  const parts = query.data?.stages.find((stage) => stage.id === "picture_description")?.prompts.filter((part) => part.id !== "template") ?? [];
  if (!parts.length) return null;
  return (
    <Disclosure summary={t("settings.answerPrompts.fixedRules")} surface="sunken" contentClassName="space-y-3" data-testid="vision-prompt-parts">
      <p className="text-sm text-fg-muted">{t("settings.answerPrompts.visionInstructions.description")}</p>
      {parts.map((part) => (
        part.prompt_key ? <AnswerPromptPanel
          key={part.id}
          promptKey={part.prompt_key}
          fieldLabel={t(`settings.answerPrompts.part.${part.id}` as I18nKey)}
        /> : <TextareaField key={part.id} id={`picture-prompt-rule-${part.id}`} label={t(`settings.answerPrompts.part.${part.id}` as I18nKey)} value={part.content} readOnly monospace rows={12} />
      ))}
    </Disclosure>
  );
}
