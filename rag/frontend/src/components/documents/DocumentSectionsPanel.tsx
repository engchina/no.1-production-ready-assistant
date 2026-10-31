import {
  Banner,
  Button,
  Disclosure,
  FormActionBar,
  FormStatus,
  INFORMATION_LIST_SCROLL_CLASS,
  INFORMATION_TABLE_FOCUS_CLASS,
  ListSkeleton,
  RowActionMenu,
  StatusBadge,
  TextField,
  TimedLoadingState,
  toast,
  type EntityAction,
} from "@production-ready/ui";
import {
  ArrowDown,
  ArrowUp,
  IndentDecrease,
  IndentIncrease,
  ListPlus,
  Pencil,
  Plus,
  RotateCcw,
  Save,
  Trash2,
  Undo2,
} from "lucide-react";
import { useState } from "react";

import { ApiErrorState } from "@/components/StateViews";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { ApiError, type DocumentSection } from "@/lib/api";
import {
  canIndent,
  canMoveDown,
  canMoveUp,
  canOutdent,
  childCount,
  indent,
  insertAfter,
  insertChild,
  moveDown,
  moveUp,
  newSection,
  outdent,
  removeSubtree,
  sectionErrors,
  sectionPageLabel,
  updateSection,
  unmatchedSummaryCount,
} from "@/lib/document-sections";
import { t } from "@/lib/i18n";
import { useLeaveGuard } from "@/lib/leave-guard";
import {
  useDocumentSections,
  useResetDocumentSections,
  useSaveDocumentSections,
} from "@/lib/queries";

let newSectionSerial = 0;

/** 追加した章節の id（保存の前に行を区別するだけ。http の画面でも使えるよう crypto に頼らない）。 */
function newSectionId(): string {
  newSectionSerial += 1;
  return `manual-${Date.now().toString(36)}-${newSectionSerial}`;
}

function parsePage(value: string): number | null {
  const page = Number.parseInt(value, 10);
  return Number.isFinite(page) ? page : null;
}

// 編集の行の段組み（名前・開始・終了・操作）。狭い幅では名前を 1 行目、ページと操作を 2 行目に置く。
const EDIT_ROW_GRID =
  "grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto] items-start gap-2 sm:grid-cols-[minmax(0,1fr)_5.5rem_5.5rem_auto]";

function indentStyle(level: number) {
  return { paddingLeft: `calc(var(--space-4) * ${Math.max(0, level - 1)})` };
}

/**
 * 文書の章節ナビゲーション（#713）。各章節のページ範囲を出し、押すとプレビューをそのページへ移す。
 * 「編集」で名前・ページ範囲・並び・階層を直し、追加・削除できる。修正は文書ごとに保存し、すべての
 * 処理レシピで共有する。修正が無ければ抽出結果の章節を出す。
 */
export function DocumentSectionsPanel({
  documentId,
  recipeId,
  summaries,
  onPageSelect,
}: {
  documentId: string;
  recipeId: string | null;
  /** 抽出の章節の要約（ナビゲーション要約が有効なとき。抽出の章節 id → 要約）。 */
  summaries?: ReadonlyMap<string, string>;
  onPageSelect?: (page: number) => void;
}) {
  const query = useDocumentSections(documentId, recipeId);
  const save = useSaveDocumentSections(documentId);
  const reset = useResetDocumentSections(documentId);
  const confirm = useConfirm();
  // 編集中の章節（null は表示だけ）。
  const [draft, setDraft] = useState<DocumentSection[] | null>(null);
  const [submitted, setSubmitted] = useState(false);
  const [saveError, setSaveError] = useState("");
  const data = query.data;
  const sections = draft ?? data?.sections ?? [];
  const pageCount = data?.page_count ?? null;
  const errors = sectionErrors(sections, pageCount);
  // 編集を始めた後に変えた章節があるときだけ離脱を確認する（#721）。
  useLeaveGuard(draft !== null && draft !== data?.sections, save.isPending);

  const startEdit = () => {
    setDraft(data?.sections ?? []);
    setSubmitted(false);
    setSaveError("");
  };
  const discard = () => {
    setDraft(null);
    setSubmitted(false);
    setSaveError("");
  };
  const change = (next: DocumentSection[]) => setDraft(next);

  const submit = () => {
    if (!draft || !data) return;
    setSubmitted(true);
    if (errors.size > 0) {
      setSaveError(t("sections.error.fix"));
      return;
    }
    setSaveError("");
    save.mutate(
      { sections: draft, base_revision: data.revision, recipe_id: recipeId },
      {
        onSuccess: () => {
          discard();
          toast.success(t("sections.toast.saved"));
        },
        onError: (error) =>
          setSaveError(error instanceof ApiError ? error.message : t("sections.error.saveFailed")),
      }
    );
  };

  const resetToExtraction = async () => {
    const confirmed = await confirm({
      title: t("sections.reset.confirm.title"),
      description: t("sections.reset.confirm.description"),
      confirmLabel: t("sections.reset.action"),
      tone: "danger",
    });
    if (!confirmed) return;
    reset.mutate(recipeId, {
      onSuccess: () => {
        discard();
        toast.success(t("sections.toast.reset"));
      },
      onError: (error) =>
        setSaveError(error instanceof ApiError ? error.message : t("sections.error.saveFailed")),
    });
  };

  const removeAt = async (index: number) => {
    if (!draft) return;
    const children = childCount(draft, index);
    if (children > 0) {
      // 子を含む削除だけ確認する（1 行の削除は「変更を破棄」で戻せる）。
      const confirmed = await confirm({
        title: t("sections.delete.confirm.title", { title: draft[index].title }),
        description: t("sections.delete.confirm.description", { count: children }),
        confirmLabel: t("sections.action.delete"),
        tone: "danger",
      });
      if (!confirmed) return;
    }
    change(removeSubtree(draft, index));
  };

  const rowActions = (index: number): EntityAction[] => {
    if (!draft) return [];
    const section = draft[index];
    return [
      { id: "up", label: t("sections.action.moveUp"), icon: ArrowUp, disabled: !canMoveUp(draft, index), onSelect: () => change(moveUp(draft, index)) },
      { id: "down", label: t("sections.action.moveDown"), icon: ArrowDown, disabled: !canMoveDown(draft, index), onSelect: () => change(moveDown(draft, index)) },
      { id: "outdent", label: t("sections.action.outdent"), icon: IndentDecrease, disabled: !canOutdent(draft, index), onSelect: () => change(outdent(draft, index)) },
      { id: "indent", label: t("sections.action.indent"), icon: IndentIncrease, disabled: !canIndent(draft, index), onSelect: () => change(indent(draft, index)) },
      { id: "after", label: t("sections.action.addAfter"), icon: Plus, onSelect: () => change(insertAfter(draft, index, newSection(newSectionId(), section.level))) },
      { id: "child", label: t("sections.action.addChild"), icon: ListPlus, onSelect: () => change(insertChild(draft, index, newSection(newSectionId(), section.level + 1))) },
      { id: "delete", label: t("sections.action.delete"), icon: Trash2, tone: "danger", onSelect: () => void removeAt(index) },
    ];
  };

  const manual = data?.source === "manual";
  const matchingSummaries = data?.summary_rules_match === false ? undefined : summaries;
  const unmatched = data?.summary_rules_match === false ? summaries?.size ?? 0 : data
    ? unmatchedSummaryCount(data.sections, summaries)
    : 0;
  return (
    <Disclosure
      summary={t("flow.extraction.navigation.title")}
      meta={
        <span className="flex items-center gap-2">
          {manual ? <StatusBadge variant="info" label={t("sections.badge.manual")} /> : null}
          <span className="tnum text-xs font-normal text-fg-muted">{sections.length}</span>
        </span>
      }
      defaultOpen
      data-testid="extraction-navigation"
    >
      {query.isPending ? (
        <TimedLoadingState label={t("sections.loading")} operationKey={`sections-${documentId}`}>
          <ListSkeleton rows={4} />
        </TimedLoadingState>
      ) : query.isError ? (
        <ApiErrorState
          error={query.error}
          fallback={t("sections.error.load")}
          onRetry={() => void query.refetch()}
        />
      ) : draft === null ? (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="text-xs text-fg-muted">
              {sections.length > 0 ? t("sections.hint.view") : t("sections.empty")}
            </p>
            <Button type="button" variant="secondary" size="sm" icon={Pencil} onClick={startEdit}>
              {t("sections.action.edit")}
            </Button>
          </div>
          {unmatched > 0 ? (
            <div data-testid="sections-summary-mismatch">
              <Banner severity="warning">{t("sections.summaryMismatch", { count: unmatched })}</Banner>
            </div>
          ) : null}
          {sections.length > 0 ? (
            <ol className={`relative space-y-0.5 ${INFORMATION_LIST_SCROLL_CLASS} ${INFORMATION_TABLE_FOCUS_CLASS}`}
              tabIndex={0} data-testid="sections-navigation-scroll" aria-label={t("flow.extraction.navigation.title")}>
              {sections.map((section) => {
                const pages = sectionPageLabel(section);
                const summary = section.source_section_id
                  ? matchingSummaries?.get(section.source_section_id)
                  : undefined;
                return (
                  <li key={section.id} style={indentStyle(section.level)}>
                    <button
                      type="button"
                      className="flex w-full items-start justify-between gap-2 rounded-md px-2 py-1.5 text-left hover:bg-surface-hover disabled:cursor-default disabled:hover:bg-transparent"
                      disabled={section.page_start === null || !onPageSelect}
                      onClick={() => section.page_start !== null && onPageSelect?.(section.page_start)}
                      aria-label={
                        pages
                          ? t("sections.jump.aria", { title: section.title, pages })
                          : section.title
                      }
                    >
                      <span className="min-w-0">
                        <span className="block break-words text-sm text-fg">{section.title}</span>
                        {section.added_from_extraction ? (
                          <StatusBadge
                            variant="info"
                            label={t("sections.badge.added")}
                            className="mt-1"
                          />
                        ) : null}
                        {summary ? (
                          <span className="mt-0.5 block text-xs text-fg-muted">{summary}</span>
                        ) : null}
                      </span>
                      {pages ? (
                        <span className="tnum shrink-0 rounded-full bg-surface-sunken px-2 py-0.5 text-xs text-fg-muted">
                          {pages}
                        </span>
                      ) : null}
                    </button>
                  </li>
                );
              })}
            </ol>
          ) : null}
        </div>
      ) : (
        <div className="space-y-3" data-testid="sections-editor">
          <p className="text-xs text-fg-muted">
            {pageCount !== null
              ? t("sections.hint.edit", { pages: pageCount })
              : t("sections.hint.editNoPages")}
          </p>
          {draft.length > 0 ? (
            <div aria-hidden className={`${EDIT_ROW_GRID} text-xs font-medium text-fg-muted`}>
              <span className="col-span-3 sm:col-span-1">{t("sections.column.title")}</span>
              <span>{t("sections.column.pageStart")}</span>
              <span>{t("sections.column.pageEnd")}</span>
            </div>
          ) : null}
          {draft.length > 0 ? (
            // 列の見出しを持つ表の形の編集の行（size sm。名前・開始・終了の列の幅は列で決める）なので、欄の幅の段の
            // 検査（findFieldsOffWidthScale）の対象から外す（表の中の欄と同じ扱い。#1891）。
            <ol className={`relative space-y-2 ${INFORMATION_LIST_SCROLL_CLASS} ${INFORMATION_TABLE_FOCUS_CLASS}`}
              tabIndex={0} data-testid="sections-editor-scroll" aria-label={t("sections.editor.label")} data-field-width-exempt="">
              {draft.map((section, index) => {
                const fieldErrors = submitted ? errors.get(section.id) : undefined;
                const title = section.title.trim() || t("sections.untitled");
                return (
                  <li key={section.id} style={indentStyle(section.level)}>
                    <div className={EDIT_ROW_GRID}>
                      <div className="col-span-3 min-w-0 sm:col-span-1">
                        <TextField
                          id={`section-title-${section.id}`}
                          label={t("sections.field.title", { level: section.level })}
                          labelHidden
                          size="sm"
                          value={section.title}
                          placeholder={t("sections.field.titlePlaceholder")}
                          error={fieldErrors?.title}
                          onValueChange={(value) =>
                            change(draft.map((item, i) => (i === index ? updateSection(item, { title: value }) : item)))
                          }
                        />
                      </div>
                      <TextField
                        id={`section-start-${section.id}`}
                        label={t("sections.field.pageStart", { title })}
                        labelHidden
                        type="number"
                        inputMode="numeric"
                        min={1}
                        size="sm"
                        value={section.page_start ?? ""}
                        placeholder={t("sections.field.pageStartPlaceholder")}
                        error={fieldErrors?.page_start}
                        onValueChange={(value) =>
                          change(draft.map((item, i) => (i === index ? updateSection(item, { page_start: parsePage(value) }) : item)))
                        }
                      />
                      <TextField
                        id={`section-end-${section.id}`}
                        label={t("sections.field.pageEnd", { title })}
                        labelHidden
                        type="number"
                        inputMode="numeric"
                        min={1}
                        size="sm"
                        value={section.page_end ?? ""}
                        placeholder={t("sections.field.pageEndPlaceholder")}
                        error={fieldErrors?.page_end}
                        onValueChange={(value) =>
                          change(draft.map((item, i) => (i === index ? updateSection(item, { page_end: parsePage(value) }) : item)))
                        }
                      />
                      <RowActionMenu
                        actions={rowActions(index)}
                        ariaLabel={t("sections.actions.aria", { title })}
                        testId={`section-actions-${index}`}
                      />
                    </div>
                  </li>
                );
              })}
            </ol>
          ) : (
            <p className="text-sm text-fg-muted">{t("sections.editor.empty")}</p>
          )}
          <FormActionBar
            ariaLabel={t("sections.actions.form")}
            primaryActions={[
              {
                id: "save",
                label: t("sections.action.save"),
                icon: Save,
                loading: save.isPending,
                disabled: reset.isPending,
                onClick: submit,
              },
            ]}
            secondaryActions={[
              {
                id: "add",
                label: t("sections.action.addLast"),
                icon: Plus,
                onClick: () => change([...draft, newSection(newSectionId(), 1)]),
              },
              { id: "discard", label: t("sections.action.discard"), icon: Undo2, onClick: discard },
            ]}
            dangerActions={
              manual
                ? [
                    {
                      id: "reset",
                      label: t("sections.reset.action"),
                      icon: RotateCcw,
                      loading: reset.isPending,
                      // 保存の処理中は押せないだけにする（スピナーは押した側だけ。#819）。
                      disabled: save.isPending,
                      onClick: () => void resetToExtraction(),
                    },
                  ]
                : []
            }
            status={saveError ? <FormStatus tone="danger" message={saveError} /> : null}
          />
        </div>
      )}
    </Disclosure>
  );
}
