"use client";

import {
  PageBody,
  PageHeader,
  Button,
  Banner,
  ButtonLink,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  Disclosure,
  FieldError,
  FieldLabel,
  Skeleton,
  TimedLoadingState,
} from "@production-ready/ui";
import {
  AlertTriangle,
  Cloud,
  Database,
  FileText,
  HardDrive,
  List,
  ListChecks,
  RefreshCw,
  RotateCcw,
  Upload,
} from "lucide-react";
import { useId, useState } from "react";
import { Link } from "react-router-dom";

import { Dropzone } from "./Dropzone";
import { UploadSelectionList } from "./UploadSelectionList";
import { UploadSendingState } from "./UploadProgress";
import { KnowledgeBaseMultiSelect } from "@/components/knowledge-bases/KnowledgeBaseMultiSelect";
import { useKnowledgeBaseSelectionHealth } from "@/components/knowledge-bases/KnowledgeBaseScopePicker";
import { useAuth } from "@/components/security/AuthProvider";
import { ErrorState } from "@/components/StateViews";
import {
  type BatchUploadFailedItem,
  type UploadResult,
  type UploadStorageSettingsData,
} from "@/lib/api";
import {
  uploadErrorMessage,
  useKnowledgeBaseChoices,
  useBatchUploadDocuments,
  useUploadDocument,
  useUploadStorageSettings,
} from "@/lib/queries";
import { t } from "@/lib/i18n";
import { MENU_PERMISSIONS } from "@/lib/permissions";
import {
  DEFAULT_MAX_UPLOAD_BYTES,
  totalUploadBytes,
  type UploadProgress,
} from "@/lib/upload-requests";
import { canSubmitUpload, uploadKnowledgeBaseRequired } from "@/lib/upload-scope";
import { mergeUploadSelection } from "@/lib/upload-selection";
import { APP_ROUTES } from "@/lib/routes";
import {
  parserProfileKey,
  sourceModalityKey,
  sourcePreviewKey,
  sourceWarningKey,
} from "@/lib/source-profile-labels";

/** アップロード → 取込 → RAG 索引化を1画面で進めるワークスペース。 */
export function UploadWorkspace() {
  const [uploaded, setUploaded] = useState<UploadResult | null>(null);
  const [batchItems, setBatchItems] = useState<UploadResult[]>([]);
  const [batchFailedItems, setBatchFailedItems] = useState<BatchUploadFailedItem[]>([]);
  const [knowledgeBaseIds, setKnowledgeBaseIds] = useState<string[]>([]);
  // 送る前に選んだファイル（#701）。送り終えるまで残し、失敗したファイルは選び直せる。
  const [selectedFiles, setSelectedFiles] = useState<File[]>([]);
  const [sentFiles, setSentFiles] = useState<File[]>([]);
  const { user, hasPermission } = useAuth();
  // KB が制限された利用者は、登録先の KB を選ばないとアップロードできない（#214）。
  const knowledgeBaseRequired = uploadKnowledgeBaseRequired(user);
  const [knowledgeBaseMissing, setKnowledgeBaseMissing] = useState(false);
  const [sendingCount, setSendingCount] = useState(0);
  // 送信済み / 合計のバイト数（#306）。送信を始めるたびに 0 から数え直す。
  const [sendProgress, setSendProgress] = useState<UploadProgress | null>(null);
  // 送るファイル（送る順。上限を超えて送らないファイルは除く）。ファイルごとの進み具合に使う（#306）。
  const [sendingFiles, setSendingFiles] = useState<File[]>([]);
  const upload = useUploadDocument();
  const batchUpload = useBatchUploadDocuments();
  // 1 ファイルの上限。送信前の確認と、一括アップロードを分けて送る基準に使う（#280）。
  const maxUploadBytes = useUploadStorageSettings().data?.max_upload_bytes ?? DEFAULT_MAX_UPLOAD_BYTES;
  const isBusy = upload.isPending || batchUpload.isPending;
  const mutationError = upload.error ?? batchUpload.error;

  const reset = () => {
    setSelectedFiles([]);
    setUploaded(null);
    setBatchItems([]);
    setBatchFailedItems([]);
    upload.reset();
    batchUpload.reset();
  };

  const handleKnowledgeBaseChange = (ids: string[]) => {
    setKnowledgeBaseIds(ids);
    if (ids.length > 0) setKnowledgeBaseMissing(false);
  };

  const handleFiles = (files: File[]) => {
    if (files.length === 0) return;
    if (!canSubmitUpload(knowledgeBaseRequired, knowledgeBaseIds)) {
      // 送信前に案内し、選択欄へ移動する（backend の 400 を待たない）。
      setKnowledgeBaseMissing(true);
      // 欄の直下にエラーを出し、選択欄（先頭の入力）へフォーカスを移す（UX 契約 messaging.md §3.2.1。#541）。
      const picker = document.getElementById(UPLOAD_KNOWLEDGE_BASE_PICKER_ID);
      picker?.scrollIntoView({ block: "center" });
      picker?.querySelector<HTMLElement>("input, button")?.focus({ preventScroll: true });
      return;
    }
    setKnowledgeBaseMissing(false);
    setSentFiles(files);
    setUploaded(null);
    setBatchItems([]);
    setBatchFailedItems([]);
    upload.reset();
    batchUpload.reset();
    // 上限を超えて送らないファイルは数えない（hook の最初の通知と同じ合計）。一括アップロードは
    // 選んだ順を保ったまとまりに分けて順に送るため、この順がそのまま送る順になる。
    const sendable = files.filter((file) => file.size <= maxUploadBytes);
    setSendingCount(files.length);
    setSendingFiles(sendable);
    setSendProgress({ sentBytes: 0, totalBytes: totalUploadBytes(sendable) });
    // 上限を超える 1 ファイルは、一括アップロードの経路で送らずに失敗として示す。
    if (files.length === 1 && files[0].size <= maxUploadBytes) {
      upload.mutate(
        { file: files[0], knowledgeBaseIds, onProgress: setSendProgress },
        {
          onSuccess: (result) => {
            setSelectedFiles([]);
            setBatchItems([result]);
            setBatchFailedItems([]);
            setUploaded(result);
          },
        }
      );
      return;
    }
    batchUpload.mutate(
      { files, knowledgeBaseIds, maxUploadBytes, onProgress: setSendProgress },
      {
        onSuccess: (result) => {
          setBatchFailedItems(result.failed_items);
          // 1 件も保存できなかったときは、1 ファイルの失敗と同じく選択を残し、そのまま送り直せるようにする
          // （結果の画面へ移らないので、選択を消すと選び直す手段が無くなる。#931）。
          if (result.items.length === 0) return;
          setSelectedFiles([]);
          setBatchItems(result.items);
          setUploaded(result.items[0]);
        },
      }
    );
  };

  return (
    <div>
      <PageHeader wide title={t("nav.upload")} subtitle={t("upload.subtitle")} />
      <PageBody wide>
        {!uploaded ? (
          <>
            <UploadStorageNotice />
            <UploadKnowledgeBasePicker
              selectedIds={knowledgeBaseIds}
              onChange={handleKnowledgeBaseChange}
              disabled={isBusy}
              required={knowledgeBaseRequired}
              missing={knowledgeBaseMissing}
              canManageKnowledgeBases={hasPermission(MENU_PERMISSIONS.knowledgeBases)}
            />
            <Dropzone
              onFiles={(files) => setSelectedFiles((current) => mergeUploadSelection(current, files))}
              disabled={isBusy}
              maxUploadBytes={maxUploadBytes}
            />
            {isBusy ? null : (
              <UploadSelectionList
                files={selectedFiles}
                maxUploadBytes={maxUploadBytes}
                onRemove={(file) =>
                  setSelectedFiles((current) => current.filter((item) => item !== file))
                }
                onClear={() => setSelectedFiles([])}
                onStart={handleFiles}
              />
            )}
            {isBusy ? (
              <UploadSendingState
                fileCount={sendingCount}
                files={sendingFiles}
                progress={sendProgress}
              />
            ) : null}
            {mutationError ? <ErrorState message={uploadErrorMessage(mutationError)} /> : null}
            {/* 選択の画面に失敗が残るのは、1 件も保存できなかったときだけ（#931）。 */}
            {batchFailedItems.length > 0 ? (
              <BatchUploadFailureList failedItems={batchFailedItems} allFailed />
            ) : null}
          </>
        ) : (
          <>
            {/* アップロードの画面はアップロードだけを扱い、文書詳細を埋め込まない（#707）。取込は文書
                インデックスか各文書の詳細から始める。 */}
            <BatchUploadSummary items={batchItems} failedItems={batchFailedItems} />
            <p className="text-sm text-fg-muted">{t("upload.result.nextHint")}</p>
            <div className="flex flex-wrap items-center gap-2">
              {batchFailedItems.length > 0 ? (
                <Button
                  variant="secondary"
                  icon={RotateCcw}
                  onClick={() => {
                    const failedNames = new Set(batchFailedItems.map((item) => item.file_name));
                    const failedFiles = sentFiles.filter((file) => failedNames.has(file.name));
                    reset();
                    setSelectedFiles(failedFiles);
                  }}
                >
                  {t("upload.reselectFailed", { count: batchFailedItems.length })}
                </Button>
              ) : null}
              <Button variant="secondary" icon={Upload} onClick={reset}>
                {t("upload.uploadAnother")}
              </Button>
              <ButtonLink to={APP_ROUTES.fileList} linkComponent={Link} icon={List}>
                {t("upload.openFileList")}
              </ButtonLink>
            </div>
          </>
        )}
      </PageBody>
    </div>
  );
}

function BatchUploadSummary({
  items,
  failedItems,
}: {
  items: UploadResult[];
  failedItems: BatchUploadFailedItem[];
}) {
  // アップロードは取込ジョブを作らない（取込は文書ごとに明示して始める）。
  // そのため「処理待ち」「スキップ」ではなく、保存できた件数と重複の可能性を示す（#280）。
  const duplicateCount = items.filter((item) => item.duplicate_of_document_id).length;
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <ListChecks size={20} className="text-accent-fg" aria-hidden />
          {t("upload.batch.title")}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-3 sm:grid-cols-4">
          <BatchMetric label={t("upload.batch.total")} value={items.length + failedItems.length} />
          <BatchMetric label={t("upload.batch.uploaded")} value={items.length} />
          <BatchMetric label={t("upload.batch.duplicates")} value={duplicateCount} />
          <BatchMetric label={t("upload.batch.failed")} value={failedItems.length} />
        </div>
        <div className="bounded-scroll-area divide-y divide-border rounded-md border border-border bg-surface-sunken">
          {items.map((item) => {
            return (
              <div
                key={item.id}
                className="flex flex-col gap-3 px-3 py-3 sm:flex-row sm:items-center sm:justify-between"
              >
                <div className="flex min-w-0 items-start gap-2">
                  {/* ファイル名の 1 行目の中心にそろえる（#2371）。 */}
                  <span className="pr-icon-line text-sm text-accent-fg">
                    <FileText size={16} aria-hidden />
                  </span>
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium text-fg" title={item.file_name}>
                      {item.file_name}
                    </p>
                    <p className="mt-1 text-xs text-fg-muted">
                      {t("sourceProfile.parser")}: {t(parserProfileKey(item.source_profile.parser_profile))}
                    </p>
                    {item.parser_notice ? (
                      <p className="mt-1 flex items-center gap-1 text-xs text-warning-fg">
                        <AlertTriangle size={14} className="shrink-0" aria-hidden />
                        {t(
                          OFFICE_CONVERSION_NOTICE_CODES.has(item.parser_notice.code)
                            ? "upload.parserNotice.officeConversion"
                            : "upload.parserNotice.short"
                        )}
                      </p>
                    ) : null}
                  </div>
                </div>
                <div className="flex shrink-0 flex-wrap items-center gap-2">
                  <ButtonLink
                    to={`${APP_ROUTES.documents}/${encodeURIComponent(item.id)}`}
                    linkComponent={Link}
                    size="sm"
                    icon={FileText}
                    aria-label={t("upload.batch.open", { name: item.file_name })}
                  >
                    {t("upload.batch.openShort")}
                  </ButtonLink>
                </div>
              </div>
            );
          })}
        </div>
        {failedItems.length > 0 ? <BatchUploadFailureList failedItems={failedItems} /> : null}
      </CardContent>
    </Card>
  );
}

function BatchUploadFailureList({
  failedItems,
  allFailed = false,
}: {
  failedItems: BatchUploadFailedItem[];
  /** 1 件も保存できなかった（処理の失敗として danger で示す。messaging.md §10.2。#931）。 */
  allFailed?: boolean;
}) {
  return (
    <Banner
      severity={allFailed ? "danger" : "warning"}
      title={t(allFailed ? "upload.batch.allFailedTitle" : "upload.batch.failedTitle")}
    >
      <ul className="bounded-scroll-area space-y-2 pr-1 text-sm">
        {failedItems.map((item, index) => (
          // 同じ名前・同じ理由で失敗したファイルが並んでも key が重ならないよう、位置を含める。
          <li key={`${index}-${item.file_name}`} className="min-w-0">
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
              <span className="font-medium">{item.file_name}</span>
              <span>{item.message}</span>
            </div>
            {item.source_profile ? (
              <div className="mt-1.5 flex flex-wrap gap-1.5 text-xs">
                <span className="rounded-full border border-border bg-surface px-2 py-0.5 text-fg-muted">
                  {t(sourceModalityKey(item.source_profile.modality))}
                </span>
                <span className="rounded-full border border-border bg-surface px-2 py-0.5 text-fg-muted">
                  {t("sourceProfile.parser")}:{" "}
                  {t(parserProfileKey(item.source_profile.parser_profile))}
                </span>
                <span className="rounded-full border border-border bg-surface px-2 py-0.5 text-fg-muted">
                  {t("sourceProfile.previewKind")}:{" "}
                  {t(sourcePreviewKey(item.source_profile.preview_kind))}
                </span>
                {item.source_profile.quality_warnings.slice(0, 2).map((warning) => (
                  <span
                    key={warning}
                    data-surface-tint="warning"
                    className="rounded-full border border-warning-border bg-warning-subtle px-2 py-0.5 text-warning-fg"
                  >
                    {t(sourceWarningKey(warning))}
                  </span>
                ))}
              </div>
            ) : null}
            {/* HTTP の状態コードは技術的な詳細なので「詳細」に畳む（一部失敗の warning では閉じる。messaging.md §10.3）。 */}
            <Disclosure
              variant="plain"
              size="sm"
              summary={t("upload.batch.failedDetails")}
              className="mt-1"
            >
              <p className="tnum text-xs text-fg-muted">
                {t("upload.batch.failedStatusCode", { status: item.status_code })}
              </p>
            </Disclosure>
          </li>
        ))}
      </ul>
    </Banner>
  );
}

function BatchMetric({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-md border border-border bg-surface-sunken px-3 py-2">
      <p className="text-xs text-fg-muted">{label}</p>
      <p className="tnum mt-1 text-lg font-semibold text-fg">{value}</p>
    </div>
  );
}

/** 原本のままでは解析できず、ファイル準備で変換が要る形式の案内のコード（backend の parser_source_guard。#2470）。 */
const OFFICE_CONVERSION_NOTICE_CODES = new Set(["office_conversion_required", "office_to_pdf_spreadsheet_not_supported"]);

const UPLOAD_KNOWLEDGE_BASE_PICKER_ID = "upload-knowledge-base-picker";
const UPLOAD_KNOWLEDGE_BASE_INPUT_ID = "upload-knowledge-base-input";
const UPLOAD_KNOWLEDGE_BASE_LABEL_ID = "upload-knowledge-base-label";

function UploadKnowledgeBasePicker({
  selectedIds,
  onChange,
  disabled,
  required,
  missing,
  canManageKnowledgeBases,
}: {
  selectedIds: string[];
  onChange: (ids: string[]) => void;
  disabled: boolean;
  /** 登録先の KB の選択が必須か（KB が制限された利用者。#214）。 */
  required: boolean;
  /** 必須なのに未選択のままファイルを選んだ。 */
  missing: boolean;
  /** ナレッジベース管理の画面を開けるか（開けない利用者には導線を出さない）。 */
  canManageKnowledgeBases: boolean;
}) {
  // 200 件以下は全件を手元で絞り込み、超えるとサーバー側で検索する（全件を読まない。#578）。
  const [q, setQ] = useState("");
  const query = useKnowledgeBaseChoices({ status: "ACTIVE", q });
  const hasKnowledgeBases = query.total > 0;
  const selection = useKnowledgeBaseSelectionHealth(selectedIds);
  const errorId = useId();

  return (
    <Card id={UPLOAD_KNOWLEDGE_BASE_PICKER_ID} data-testid="upload-knowledge-base-picker">
      <CardHeader>
        <CardTitle>
          {/* 必須は入力欄（combobox）の aria-required で伝える。タグは FieldLabel が読み上げから外す */}
          <FieldLabel
            id={UPLOAD_KNOWLEDGE_BASE_LABEL_ID}
            htmlFor={UPLOAD_KNOWLEDGE_BASE_INPUT_ID}
            label={t("upload.knowledgeBases.title")}
            required={required}
            className="font-semibold"
          />
        </CardTitle>
      </CardHeader>
      <CardContent>
        {query.isPending ? (
          <TimedLoadingState
            label={t("upload.knowledgeBases.loading")}
            placement="panel"
            testId="upload-knowledge-base-loading"
          >
            <Skeleton className="h-8 w-full" />
          </TimedLoadingState>
        ) : query.isError ? (
          // 取得に失敗したまま（必須の利用者は）送れないため、案内とその場の再読み込みを出す。
          <Banner severity="warning" title={t("upload.knowledgeBases.loadWarning")}>
            <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
              <p>
                {required
                  ? t("upload.knowledgeBases.loadWarningRequiredHint")
                  : t("upload.knowledgeBases.loadWarningHint")}
              </p>
              <Button
                type="button"
                variant="secondary"
                size="sm"
                icon={RefreshCw}
                loading={query.isFetching}
                onClick={() => void query.refetch()}
              >
                {t("common.retry")}
              </Button>
            </div>
          </Banner>
        ) : hasKnowledgeBases ? (
          <KnowledgeBaseMultiSelect
            id={UPLOAD_KNOWLEDGE_BASE_INPUT_ID}
            width="lg"
            label={t("upload.knowledgeBases.title")}
            labelHidden
            labelledBy={UPLOAD_KNOWLEDGE_BASE_LABEL_ID}
            required={required}
            invalid={missing}
            describedBy={missing ? errorId : undefined}
            choices={query}
            onQueryChange={setQ}
            selectedIds={selectedIds}
            onChange={onChange}
            selectedItems={selection.items}
            disabled={disabled}
          />
        ) : required ? (
          <p className="rounded-md border border-border bg-surface-sunken p-4 text-sm text-fg-muted">
            {t("upload.knowledgeBases.emptyRestrictedHint")}
          </p>
        ) : (
          <div className="flex flex-col gap-3 rounded-md border border-border bg-surface-sunken p-4 text-sm text-fg-muted sm:flex-row sm:items-center sm:justify-between">
            <span>{t("upload.knowledgeBases.emptyHint")}</span>
            {canManageKnowledgeBases ? (
              <ButtonLink
                to={APP_ROUTES.knowledgeBases}
                linkComponent={Link}
                size="sm"
                icon={Database}
              >
                {t("upload.knowledgeBases.manage")}
              </ButtonLink>
            ) : null}
          </div>
        )}
        {/* 未選択のエラーを出している間は、同じ内容の案内を重ねない。 */}
        {hasKnowledgeBases && !missing ? (
          <p className="mt-3 text-xs text-fg-muted">
            {selectedIds.length > 0
              ? t("upload.knowledgeBases.selected", { count: selectedIds.length })
              : required
                ? t("upload.knowledgeBases.requiredHint")
                : t("upload.knowledgeBases.defaultHint")}
          </p>
        ) : null}
        {missing ? (
          <FieldError
            id={errorId}
            className="mt-2"
            message={t("upload.knowledgeBases.requiredError")}
          />
        ) : null}
      </CardContent>
    </Card>
  );
}

function UploadStorageNotice() {
  const query = useUploadStorageSettings();

  if (query.isPending || query.isError || !query.data) return null;

  return (
    <div className="flex flex-col gap-3 rounded-md border border-border bg-surface px-4 py-3 text-sm text-fg md:flex-row md:items-center md:justify-between">
      <div className="flex items-start gap-3">
        <div data-surface-tint="info" className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-info-subtle text-info-fg">
          {query.data.backend === "oci" ? (
            <Cloud size={20} aria-hidden />
          ) : (
            <HardDrive size={20} aria-hidden />
          )}
        </div>
        <div>
          <p className="font-medium">
            {t("upload.storageNotice.title")}: {storageBackendLabel(query.data.backend)}
          </p>
          <p className="mt-1 break-all text-xs text-fg-muted">
            {storageTarget(query.data)}
          </p>
        </div>
      </div>

    </div>
  );
}

function storageBackendLabel(backend: UploadStorageSettingsData["backend"]): string {
  return backend === "oci"
    ? t("settings.uploadStorage.backend.oci")
    : t("settings.uploadStorage.backend.local");
}

function storageTarget(settings: UploadStorageSettingsData): string {
  if (settings.backend === "oci") {
    return settings.object_storage_namespace && settings.object_storage_bucket
      ? `${settings.object_storage_namespace}/${settings.object_storage_bucket}`
      : t("upload.storageNotice.unset");
  }
  return settings.local_storage_dir || t("upload.storageNotice.unset");
}
