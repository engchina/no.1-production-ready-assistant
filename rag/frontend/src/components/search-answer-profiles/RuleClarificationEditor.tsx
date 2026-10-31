import {
  Button,
  FormActionBar,
  FormGrid,
  FormStatus,
  SearchableSelectField,
  Switch,
  TextareaField,
  TextField,
  toast,
} from "@production-ready/ui";
import { useQueryClient } from "@tanstack/react-query";
import { MessageCircleQuestion, Plus, Save, Trash2, Undo2, X } from "lucide-react";
import { useState } from "react";

import { useConfirm } from "@/components/ui/confirm-dialog";
import {
  api,
  ApiError,
  type ClarificationOption,
  type ClarificationSection,
  type JsonValue,
  type RuleClarification,
} from "@/lib/api";
import { sectionPageLabel } from "@/lib/document-sections";
import { t } from "@/lib/i18n";
import { useDocumentCandidates, useDocumentSections } from "@/lib/queries";

const MIN_OPTIONS = 2;
const MAX_OPTIONS = 8;
let optionSerial = 0;

function newOption(): ClarificationOption {
  optionSerial += 1;
  return {
    id: `opt-${Date.now().toString(36)}-${optionSerial}`,
    label: "",
    description: "",
    search_terms: [],
    premise: "",
    sections: [],
  };
}

function newClarification(): RuleClarification {
  return { question: "", multiple: true, allow_other: true, options: [newOption(), newOption()] };
}

/** 保存済みのルールの行の `clarification`（壊れた値は無いものとして扱う）。 */
export function storedClarification(value: JsonValue | undefined): RuleClarification | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const candidate = value as unknown as RuleClarification;
  return typeof candidate.question === "string" && Array.isArray(candidate.options)
    ? candidate
    : null;
}

function clarificationErrors(draft: RuleClarification) {
  return {
    question: draft.question.trim() ? undefined : t("clarification.error.question"),
    options: draft.options.map((option) =>
      option.label.trim() ? undefined : t("clarification.error.label")
    ),
  };
}

/**
 * ルールの確認の質問と選択肢（チャットの確認。#717）。質問がこのルールに当たると、チャットは回答の前に
 * この質問を出す。選択肢に章節を指定すると、選んだときその章節のページだけから回答する。ルールの名前・
 * 内容の保存とは別に保存する。
 */
export function RuleClarificationEditor({
  searchAnswerProfileId,
  ruleId,
  ruleTitle,
  stored,
}: {
  searchAnswerProfileId: string;
  ruleId: string;
  ruleTitle: string;
  stored: RuleClarification | null;
}) {
  const queryClient = useQueryClient();
  const confirm = useConfirm();
  const [draft, setDraft] = useState<RuleClarification | null>(stored);
  const [dirty, setDirty] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  // 保存と削除は同じ persist を使う。スピナーは押した操作だけが出し、他は無効にする（#819）。
  const [pendingOperation, setPendingOperation] = useState<"save" | "remove" | null>(null);
  const saving = pendingOperation !== null;
  const [error, setError] = useState("");
  const errors = draft ? clarificationErrors(draft) : null;

  const change = (next: RuleClarification | null) => {
    setDraft(next);
    setDirty(true);
  };
  const updateOption = (index: number, patch: Partial<ClarificationOption>) => {
    if (!draft) return;
    change({
      ...draft,
      options: draft.options.map((option, i) => (i === index ? { ...option, ...patch } : option)),
    });
  };

  const persist = async (
    operation: "save" | "remove",
    value: RuleClarification | null,
    message: string
  ) => {
    setPendingOperation(operation);
    setError("");
    try {
      await api.saveRuleClarification(searchAnswerProfileId, ruleId, value);
      await queryClient.invalidateQueries({
        queryKey: ["search-answer-profiles", searchAnswerProfileId, "runtime-knowledge"],
      });
      setDirty(false);
      setSubmitted(false);
      toast.success(message);
    } catch (failure) {
      setError(failure instanceof ApiError ? failure.message : t("clarification.error.save"));
    } finally {
      setPendingOperation(null);
    }
  };

  const save = () => {
    if (!draft) return;
    setSubmitted(true);
    if (errors?.question || errors?.options.some(Boolean)) {
      setError(t("clarification.error.fix"));
      return;
    }
    void persist(
      "save",
      {
        ...draft,
        question: draft.question.trim(),
        options: draft.options.map((option) => ({ ...option, label: option.label.trim() })),
      },
      t("clarification.toast.saved")
    );
  };

  const remove = async () => {
    const confirmed = await confirm({
      title: t("clarification.remove.confirm.title"),
      description: t("clarification.remove.confirm.description"),
      confirmLabel: t("clarification.remove.action"),
      tone: "danger",
    });
    if (!confirmed) return;
    setDraft(null);
    void persist("remove", null, t("clarification.toast.removed"));
  };

  return (
    <section
      aria-labelledby="rule-clarification-title"
      className="space-y-3 rounded-md border border-border p-3"
      data-testid="rule-clarification-editor"
    >
      <h4 id="rule-clarification-title" className="flex items-center gap-2 text-sm font-semibold text-fg">
        <MessageCircleQuestion size={16} className="text-accent-fg" aria-hidden />
        {t("clarification.title", { rule: ruleTitle })}
      </h4>
      <p className="text-xs leading-relaxed text-fg-muted">{t("clarification.description")}</p>

      {draft === null ? (
        <Button
          type="button"
          variant="secondary"
          size="sm"
          icon={Plus}
          onClick={() => change(newClarification())}
        >
          {t("clarification.add")}
        </Button>
      ) : (
        <>
          {/* 聞き返しの文は文章なので full（フォームの最大幅まで。#1891）。 */}
          <FormGrid columns={1}>
            <TextField
              id="rule-clarification-question"
              width="full"
              label={t("clarification.question")}
              required
              value={draft.question}
              placeholder={t("clarification.questionPlaceholder")}
              error={submitted ? errors?.question : undefined}
              onValueChange={(question) => change({ ...draft, question })}
            />
          </FormGrid>
          <div className="flex flex-wrap gap-x-6 gap-y-2">
            <label className="flex items-center gap-2 text-sm text-fg">
              <Switch
                checked={draft.multiple}
                aria-label={t("clarification.multiple")}
                onCheckedChange={(multiple) => change({ ...draft, multiple })}
              />
              {t("clarification.multiple")}
            </label>
          </div>
          <ol className="space-y-3" aria-label={t("clarification.options")}>
            {draft.options.map((option, index) => (
              <li key={option.id} className="space-y-2 rounded-md border border-border bg-surface-sunken p-3">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-xs font-semibold text-fg-muted">
                    {t("clarification.optionNumber", { number: index + 1 })}
                  </span>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    icon={Trash2}
                    disabled={draft.options.length <= MIN_OPTIONS}
                    aria-label={t("clarification.removeOption", { number: index + 1 })}
                    onClick={() =>
                      change({ ...draft, options: draft.options.filter((_, i) => i !== index) })
                    }
                  >
                    {t("clarification.removeOptionShort")}
                  </Button>
                </div>
                {/* 選択肢の名前は md、説明・検索語・前提は長い値なので全幅（full。#1891）。 */}
                <FormGrid>
                  <TextField
                    id={`clarification-label-${option.id}`}
                    width="md"
                    label={t("clarification.label")}
                    required
                    value={option.label}
                    error={submitted ? errors?.options[index] : undefined}
                    onValueChange={(label) => updateOption(index, { label })}
                  />
                  <TextField
                    id={`clarification-description-${option.id}`}
                    className="col-span-full"
                    width="full"
                    label={t("clarification.optionDescription")}
                    value={option.description}
                    onValueChange={(description) => updateOption(index, { description })}
                  />
                  <TextField
                    id={`clarification-terms-${option.id}`}
                    className="col-span-full"
                    width="full"
                    label={t("clarification.searchTerms")}
                    value={option.search_terms.join("、")}
                    helper={t("clarification.searchTermsHelp")}
                    onValueChange={(value) =>
                      updateOption(index, { search_terms: value.split(/[、,，\n]/).map((term) => term.trim()).filter(Boolean) })
                    }
                  />
                  <TextareaField
                    id={`clarification-premise-${option.id}`}
                    className="col-span-full"
                    width="full"
                    label={t("clarification.premise")}
                    rows={2}
                    value={option.premise}
                    onValueChange={(premise) => updateOption(index, { premise })}
                  />
                </FormGrid>
                <OptionSections
                  sections={option.sections}
                  onChange={(sections) => updateOption(index, { sections })}
                />
              </li>
            ))}
          </ol>
          <Button
            type="button"
            variant="secondary"
            size="sm"
            icon={Plus}
            disabled={draft.options.length >= MAX_OPTIONS}
            onClick={() => change({ ...draft, options: [...draft.options, newOption()] })}
          >
            {t("clarification.addOption")}
          </Button>
        </>
      )}

      {draft !== null || dirty ? (
        <FormActionBar
          ariaLabel={t("clarification.actions")}
          primaryActions={[
            {
              id: "save",
              label: t("clarification.save"),
              icon: Save,
              loading: pendingOperation === "save",
              disabled: draft === null || !dirty || pendingOperation === "remove",
              onClick: save,
            },
          ]}
          secondaryActions={[
            {
              id: "discard",
              label: t("clarification.discard"),
              icon: Undo2,
              disabled: !dirty || saving,
              onClick: () => {
                setDraft(stored);
                setDirty(false);
                setSubmitted(false);
                setError("");
              },
            },
          ]}
          dangerActions={
            stored
              ? [
                  {
                    id: "remove",
                    label: t("clarification.remove.action"),
                    icon: X,
                    loading: pendingOperation === "remove",
                    disabled: pendingOperation === "save",
                    onClick: () => void remove(),
                  },
                ]
              : []
          }
          status={error ? <FormStatus tone="danger" message={error} /> : null}
        />
      ) : null}
    </section>
  );
}

/** 選択肢が指す章節（文書を選び、章節にチェックを付けて足す）。ページは足したときの章節の範囲。 */
function OptionSections({
  sections,
  onChange,
}: {
  sections: ClarificationSection[];
  onChange: (sections: ClarificationSection[]) => void;
}) {
  const [documentQuery, setDocumentQuery] = useState("");
  const [documentId, setDocumentId] = useState("");
  const documents = useDocumentCandidates({ q: documentQuery || undefined });
  const [selectedDocument, setSelectedDocument] = useState<{ value: string; label: string } | null>(null);
  // API がページング前に名前の自然順で並べる。追加取得の重複だけを ID で除く。
  const candidates = [
    ...new Map(
      (documents.data?.pages ?? [])
        .flatMap((page) => page.items)
        .map((item) => [item.id, item]),
    ).values(),
  ];
  const documentOptions = candidates.map((item) => ({
    value: item.id,
    label: item.file_name,
  }));
  const document = documentOptions.find((option) => option.value === documentId) ?? selectedDocument;
  const sectionsQuery = useDocumentSections(documentId || null, null);
  const chosen = new Set(sections.map((section) => `${section.document_id}:${section.section_id}`));

  const toggle = (section: ClarificationSection) => {
    const key = `${section.document_id}:${section.section_id}`;
    onChange(
      chosen.has(key)
        ? sections.filter((item) => `${item.document_id}:${item.section_id}` !== key)
        : [...sections, section]
    );
  };

  return (
    <div className="space-y-2">
      <p className="text-xs font-semibold text-fg">{t("clarification.sections")}</p>
      {sections.length > 0 ? (
        <ul className="flex flex-wrap gap-2" aria-label={t("clarification.sectionsChosen")}>
          {sections.map((section) => (
            <li
              key={`${section.document_id}:${section.section_id}`}
              className="flex items-center gap-1 rounded-full border border-border bg-surface px-2 py-0.5 text-xs text-fg"
            >
              <span className="break-all">
                「{section.document_name || section.document_id}」{section.title}
                {sectionPageLabel(section) ? ` ${sectionPageLabel(section)}` : ""}
              </span>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                iconOnly
                icon={X}
                aria-label={t("clarification.removeSection", { title: section.title })}
                onClick={() => toggle(section)}
              />
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-xs text-fg-muted">{t("clarification.sectionsEmpty")}</p>
      )}
      <SearchableSelectField
        id={`clarification-document-${sections.length}-${documentId}`}
        label={t("clarification.document")}
        value={documentId}
        options={documentOptions}
        selectedOption={document}
        onValueChange={(value) => {
          setDocumentId(value);
          setSelectedDocument(documentOptions.find((option) => option.value === value) ?? null);
        }}
        onQueryChange={setDocumentQuery}
        remote={{
          total: documents.data?.pages.at(-1)?.total ?? documentOptions.length,
          hasMore: documents.hasNextPage,
          loadingMore: documents.isFetchingNextPage,
          searching: documents.isFetching && !documents.isFetchingNextPage,
          onLoadMore: () => void documents.fetchNextPage(),
        }}
        // 文書（対象）の選択（lg。#1891）。
        width="lg"
      />
      {documentId && sectionsQuery.data ? (
        sectionsQuery.data.sections.length > 0 ? (
          <ul className="max-h-60 space-y-1 overflow-y-auto rounded-md border border-border bg-surface p-2">
            {sectionsQuery.data.sections.map((section) => {
              const item: ClarificationSection = {
                document_id: documentId,
                document_name: document?.label ?? "",
                section_id: section.id,
                title: section.title,
                page_start: section.page_start,
                page_end: section.page_end,
              };
              const pages = sectionPageLabel(section);
              return (
                <li key={section.id} style={{ paddingLeft: `calc(var(--space-4) * ${section.level - 1})` }}>
                  <label className="flex cursor-pointer items-start gap-2 text-sm text-fg">
                    <input
                      type="checkbox"
                      className="mt-0.5"
                      checked={chosen.has(`${documentId}:${section.id}`)}
                      onChange={() => toggle(item)}
                    />
                    <span className="break-words">
                      {section.title}
                      {pages ? <span className="tnum text-xs text-fg-muted">（{pages}）</span> : null}
                    </span>
                  </label>
                </li>
              );
            })}
          </ul>
        ) : (
          <p className="text-xs text-fg-muted">{t("clarification.noSections")}</p>
        )
      ) : null}
    </div>
  );
}
