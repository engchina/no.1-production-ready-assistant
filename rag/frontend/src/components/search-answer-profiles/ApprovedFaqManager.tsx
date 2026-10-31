import { FileSpreadsheet, Plus, Trash2, Upload } from "lucide-react";
import { useState } from "react";

import {
  Button,
  EmptyState,
  FieldLabel,
  FormGrid,
  FormStatus,
  ListToolbar,
  PAGE_SIZE_OPTIONS,
  ProcessingIndicator,
  RowActionMenu,
  SelectField,
  TableSkeleton,
  TextareaField,
  TextField,
  Switch,
  TimedLoadingState,
  toast,
  useConfirm,
  type SelectFieldOption,
  PagedDataTable,
} from "@production-ready/ui";

import {
  api,
  ApiError,
  type ApprovedFaqImportMode,
  type ApprovedFaqImportPreviewData,
  type ApprovedFaqRecordData,
} from "@/lib/api";
import { listCountProps, useClientListPaging } from "@/lib/list-paging";
import { paginationLabels } from "@/lib/pagination-labels";
import { ErrorState } from "@/components/StateViews";
import { t } from "@/lib/i18n";
import { useLeaveGuard } from "@/lib/leave-guard";
import { focusFirstInvalidField, requiredTextError } from "@/lib/required-fields";
import { initialLoadError, useApprovedFaq, useApprovedFaqMutation } from "@/lib/queries";

const IMPORT_MODE_OPTIONS: SelectFieldOption<ApprovedFaqImportMode>[] = [
  { value: "INSERT", label: t("searchAnswerProfiles.faq.importMode.INSERT") },
  {
    value: "DELETE_THEN_INSERT",
    label: t("searchAnswerProfiles.faq.importMode.DELETE_THEN_INSERT"),
  },
];

function errorMessage(error: unknown, fallback: string) {
  return error instanceof ApiError ? error.message : fallback;
}

/** 検索・回答プロファイルの Approved FAQ(類似問)の一覧・追加・削除・Excel 取込。 */
export function ApprovedFaqManager({
  searchAnswerProfileId,
}: {
  searchAnswerProfileId: string;
}) {
  const query = useApprovedFaq(searchAnswerProfileId);
  const records = query.data?.records ?? [];
  const paging = useClientListPaging("approvedFaqs.listPaging", searchAnswerProfileId);
  // 初回の読み込みの失敗は、空（0 件・既定のオン）と見せずに失敗と再試行を出す。
  const loadError = initialLoadError(query);
  const add = useApprovedFaqMutation(
    searchAnswerProfileId,
    (body: { question: string; answer: string }) =>
      api.addApprovedFaq(searchAnswerProfileId, body),
  );
  const remove = useApprovedFaqMutation(searchAnswerProfileId, (ids: string[]) =>
    api.deleteApprovedFaq(searchAnswerProfileId, ids),
  );
  const setEnabled = useApprovedFaqMutation(searchAnswerProfileId, (enabled: boolean) =>
    api.setApprovedFaqEnabled(searchAnswerProfileId, enabled),
  );
  const importFaq = useApprovedFaqMutation(
    searchAnswerProfileId,
    (args: { file: File; mode: ApprovedFaqImportMode }) =>
      api.importApprovedFaq(searchAnswerProfileId, args.file, args.mode),
  );
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState("");
  const [addErrors, setAddErrors] = useState<{ question?: string | null; answer?: string | null }>({});
  const [file, setFile] = useState<File | null>(null);
  const [mode, setMode] = useState<ApprovedFaqImportMode>("INSERT");
  const [preview, setPreview] = useState<ApprovedFaqImportPreviewData | null>(
    null,
  );
  const [previewError, setPreviewError] = useState("");
  const confirm = useConfirm();

  // FAQ の削除は取り消せないため、行のメニューから確認ダイアログを通す（buttons.md §5.1）。
  const handleDelete = async (row: ApprovedFaqRecordData) => {
    const ok = await confirm({
      title: t("searchAnswerProfiles.faq.deleteConfirm.title"),
      description: t("searchAnswerProfiles.faq.deleteConfirm.description", {
        question: row.question,
      }),
      confirmLabel: t("searchAnswerProfiles.faq.delete"),
      tone: "danger",
      dismissOnOverlay: false,
    });
    if (!ok) return;
    remove.mutate([row.id], {
      onSuccess: () => toast.success(t("searchAnswerProfiles.faq.deleted")),
      onError: (error) =>
        toast.error(errorMessage(error, t("searchAnswerProfiles.faq.saveError"))),
    });
  };
  // 追加前の Q&A 入力があるときだけ離脱を確認する。
  useLeaveGuard(Boolean(question.trim() || answer.trim()));

  const selectFile = async (next: File | null) => {
    setFile(next);
    setPreview(null);
    setPreviewError("");
    if (!next) return;
    try {
      setPreview(await api.previewApprovedFaqImport(searchAnswerProfileId, next));
    } catch (error) {
      setPreviewError(errorMessage(error, t("searchAnswerProfiles.faq.previewError")));
    }
  };

  return (
    <div className="space-y-5">
      {/* 類似問の提示のオン / オフ（検索・回答プロファイルごと。既定はオン。#684）。 */}
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p id="approved-faq-enabled-label" className="text-sm font-medium text-fg">
            {t("searchAnswerProfiles.faq.enabled.label")}
          </p>
          <p id="approved-faq-enabled-help" className="mt-0.5 text-xs leading-relaxed text-fg-muted">
            {t("searchAnswerProfiles.faq.enabled.help")}
          </p>
        </div>
        <Switch
          checked={query.data?.enabled ?? true}
          // 読み込めていない間（読み込み中・失敗）は今の設定が分からないので切り替えさせない。
          disabled={!query.data || setEnabled.isPending}
          onCheckedChange={(checked) =>
            setEnabled.mutate(checked, {
              onError: (error) =>
                toast.error(errorMessage(error, t("searchAnswerProfiles.faq.enabled.error"))),
            })
          }
          aria-labelledby="approved-faq-enabled-label"
          aria-describedby="approved-faq-enabled-help"
          data-testid="approved-faq-enabled"
        />
      </div>
      {/* 件数は一覧の上の右に 1 か所（ListCount。#1891）。 */}
      <ListToolbar
        count={query.data ? listCountProps({ matchedTotal: records.length, filtered: false, live: true }) : undefined}
        testId="approved-faq-toolbar"
      />
      {query.isPending ? (
        <TimedLoadingState
          label={t("searchAnswerProfiles.faq.loading")}
          operationKey={`approved-faq-${searchAnswerProfileId}`}
          testId="approved-faq-loading"
        >
          <TableSkeleton columns={3} />
        </TimedLoadingState>
      ) : loadError ? (
        <ErrorState
          message={errorMessage(loadError, t("searchAnswerProfiles.faq.loadError"))}
          onRetry={() => void query.refetch()}
        />
      ) : (
      <PagedDataTable<ApprovedFaqRecordData>
        showSinglePagePagination
        paginationLabels={paginationLabels()}
        {...paging}
        pageSizeOptions={PAGE_SIZE_OPTIONS}
        paginationAriaLabel={t("searchAnswerProfiles.faq.paginationLabel")}
        columns={[
          {
            key: "question",
            header: t("searchAnswerProfiles.faq.question"),
            rowHeader: true,
            render: (row) => (
              <span className="break-words">{row.question}</span>
            ),
          },
          {
            key: "answer",
            header: t("searchAnswerProfiles.faq.answer"),
            render: (row) => (
              <span className="line-clamp-3 whitespace-pre-wrap break-words">
                {row.answer}
              </span>
            ),
          },
          {
            key: "actions",
            header: t("searchAnswerProfiles.faq.actions"),
            align: "right",
            render: (row) => (
              <RowActionMenu
                actions={[
                  {
                    id: "delete",
                    label: t("searchAnswerProfiles.faq.delete"),
                    icon: Trash2,
                    tone: "danger",
                    loading: remove.isPending && remove.variables?.includes(row.id),
                    onSelect: () => handleDelete(row),
                  },
                ]}
                ariaLabel={t("common.objectActions.aria", { name: row.question })}
                loading={remove.isPending && remove.variables?.includes(row.id)}
                testId={`approved-faq-row-actions-${row.id}`}
              />
            ),
          },
        ]}
        rows={records}
        getRowKey={(row) => row.id}
        dense
        empty={
          <EmptyState
            title={t("searchAnswerProfiles.faq.empty")}
            hint={t("searchAnswerProfiles.faq.emptyHint")}
          />
        }
        ariaLabel={t("searchAnswerProfiles.faq.listAria")}
        scrollAriaLabel={t("searchAnswerProfiles.faq.scrollLabel")}
        scrollTestId="approved-faq-scroll-region"
        paginationTestId="approved-faq-pagination"
      />
      )}

      <div className="grid min-w-0 grid-cols-1 gap-4 lg:grid-cols-2">
        <section className="min-w-0 space-y-3 rounded-md border border-border p-3">
          <h4 className="text-sm font-semibold text-fg">
            {t("searchAnswerProfiles.faq.addTitle")}
          </h4>
          {/* 質問・回答は文章（全幅。#1891）。 */}
          <FormGrid columns={1}>
          <TextField
            id="approved-faq-question"
            width="full"
            label={t("searchAnswerProfiles.faq.question")}
            value={question}
            onChange={(event) => {
              setQuestion(event.target.value);
              setAddErrors((current) => ({ ...current, question: null }));
            }}
            maxLength={1000}
            error={addErrors.question ?? undefined}
            required
          />
          <TextareaField
            id="approved-faq-answer"
            label={t("searchAnswerProfiles.faq.answer")}
            required
            error={addErrors.answer ?? undefined}
            value={answer}
            onChange={(event) => {
              setAnswer(event.target.value);
              setAddErrors((current) => ({ ...current, answer: null }));
            }}
            rows={4}
            maxLength={20000}
            width="full"
          />
          </FormGrid>
          {/* 追加のボタンは押せる状態のまま、未入力は押したときに欄の直下へ出す（#541）。文言は backend と同じ。 */}
          <Button
            size="sm"
            icon={Plus}
            loading={add.isPending}
            onClick={() => {
              const nextErrors = {
                question: requiredTextError(question, t("searchAnswerProfiles.faq.error.questionRequired")),
                answer: requiredTextError(answer, t("searchAnswerProfiles.faq.error.answerRequired")),
              };
              setAddErrors(nextErrors);
              if (
                focusFirstInvalidField([
                  ["approved-faq-question", nextErrors.question],
                  ["approved-faq-answer", nextErrors.answer],
                ])
              ) {
                return;
              }
              add.mutate(
                { question: question.trim(), answer: answer.trim() },
                {
                  onSuccess: () => {
                    setQuestion("");
                    setAnswer("");
                    toast.success(t("searchAnswerProfiles.faq.added"));
                  },
                  onError: (error) =>
                    toast.error(
                      errorMessage(error, t("searchAnswerProfiles.faq.saveError")),
                    ),
                },
              );
            }}
          >
            {t("searchAnswerProfiles.faq.add")}
          </Button>
        </section>

        <section className="min-w-0 space-y-3 rounded-md border border-border p-3">
          <h4 className="flex items-center gap-1.5 text-sm font-semibold text-fg">
            <FileSpreadsheet size={16} aria-hidden />
            {t("searchAnswerProfiles.faq.importTitle")}
          </h4>
          <p className="text-xs leading-relaxed text-fg-muted">
            {t("searchAnswerProfiles.faq.importHelp")}
          </p>
          {/* 取込はファイルを選ぶまで実行できない（取込モードは既定値があるので必須にしない） */}
          <FieldLabel
            htmlFor="approved-faq-import-file"
            label={t("searchAnswerProfiles.faq.importFile")}
            required
            className="block"
          />
          <input
            id="approved-faq-import-file"
            type="file"
            accept=".xlsx,.xls"
            required
            onChange={(event) =>
              void selectFile(event.target.files?.[0] ?? null)
            }
            className="block w-full text-sm text-fg file:mr-3 file:rounded-md file:border file:border-border-control file:bg-surface file:px-3 file:py-1.5 file:text-sm"
          />
          <SelectField
            id="approved-faq-import-mode"
            label={t("searchAnswerProfiles.faq.importModeLabel")}
            value={mode}
            options={IMPORT_MODE_OPTIONS}
            onValueChange={(value) => value && setMode(value)}
            width="md"
          />
          {previewError ? (
            <FormStatus tone="danger" message={previewError} />
          ) : null}
          {preview ? (
            <div className="space-y-1 text-xs text-fg-muted">
              <p>
                {t("searchAnswerProfiles.faq.previewCount", { count: preview.total })}
              </p>
              <ul className="space-y-1">
                {preview.rows.map((row) => (
                  <li key={row.row} className="break-words">
                    {row.question}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
          <Button
            size="sm"
            variant="secondary"
            icon={Upload}
            loading={importFaq.isPending}
            disabled={!file || !preview}
            onClick={() =>
              file &&
              importFaq.mutate(
                { file, mode },
                {
                  onSuccess: (data) =>
                    toast.success(
                      t("searchAnswerProfiles.faq.imported", {
                        inserted: data.inserted_count,
                        deleted: data.deleted_count,
                      }),
                    ),
                  onError: (error) =>
                    toast.error(
                      errorMessage(error, t("searchAnswerProfiles.faq.saveError")),
                    ),
                },
              )
            }
          >
            {t("searchAnswerProfiles.faq.import")}
          </Button>
          {importFaq.isPending ? (
            <ProcessingIndicator
              active
              label={t("searchAnswerProfiles.faq.importing")}
              operationKey="approved-faq-import"
              placement="action"
              activityIcon="none"
              testId="approved-faq-import-processing"
            />
          ) : null}
        </section>
      </div>
    </div>
  );
}
