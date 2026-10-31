import { Braces, Download } from "lucide-react";
import { useState } from "react";

import {
  Banner,
  buttonVariants,
  ContentActionBar,
  CopyButton,
  FormStatus,
  ListSkeleton,
  TimedLoadingState,
} from "@production-ready/ui";
import { EmptyState } from "@/components/StateViews";
import { api, type DocumentExtractionExportFormat } from "@/lib/api";
import { t, type I18nKey } from "@/lib/i18n";
import { cn } from "@/lib/utils";

import {
  EXTRACTION_EXPORT_FORMATS,
  type ExtractionExportFileFormat,
} from "./DocumentExtractionExport.logic";

const FORMAT_LABEL_KEYS: Record<ExtractionExportFileFormat, I18nKey> = {
  markdown: "flow.extractionExport.markdown",
  html: "flow.extractionExport.html",
  json: "flow.extractionExport.json",
};

const FORMAT_DESCRIPTION_KEYS: Record<ExtractionExportFileFormat, I18nKey> = {
  markdown: "flow.extractionExport.markdownDescription",
  html: "flow.extractionExport.htmlDescription",
  json: "flow.extractionExport.jsonDescription",
};

/** 共有 Button の secondary / sm と同じ見た目のダウンロードのリンク（原本プレビューの「ダウンロード」と同じ作り）。 */
const DOWNLOAD_LINK_CLASS = cn(
  buttonVariants({ variant: "secondary", size: "sm" }),
  "whitespace-nowrap"
);

/**
 * 文書の詳細の「抽出エクスポート」タブ。
 *
 * 抽出結果を Markdown / HTML / JSON のファイルにダウンロードするか、クリップボードへコピーする（#561）。
 * 内容の確認は「本文テキスト」「構造化要素」タブに任せ、ここでは本文を表示しない。
 * 読み込み中・エラーは、画面が構造化要素のために取得している JSON の抽出結果の状態を使う
 * （抽出結果が取得できない＝どの形式も持ち出せない）。
 */
export function DocumentExtractionExportPanel({
  documentId,
  recipeId,
  hasExtraction,
  loading,
  error,
}: {
  documentId: string;
  recipeId: string | null;
  /** 選んだレシピに抽出結果があるか。 */
  hasExtraction: boolean;
  loading: boolean;
  error: boolean;
}) {
  // コピーの失敗は操作した行の直下に残す（messaging §4.2。固定面があるので Toast にしない）。
  const [copyFailedFormat, setCopyFailedFormat] = useState<ExtractionExportFileFormat | null>(
    null
  );

  return (
    <section
      className="mt-4 rounded-lg border border-border bg-surface-sunken p-4"
      data-testid="document-extraction-export"
    >
      <h4 className="flex items-center gap-2 text-sm font-semibold text-fg">
        <Braces size={16} className="text-accent-fg" aria-hidden />
        {t("flow.extractionExport.title")}
      </h4>
      <p className="mt-1 text-sm leading-6 text-fg-muted">
        {t("flow.extractionExport.description")}
      </p>
      <div className="mt-3">
        {!recipeId || !hasExtraction ? (
          <div className="rounded-md border border-border bg-surface">
            <EmptyState
              title={t("flow.extractionExport.empty")}
              hint={t("flow.extractionExport.emptyHint")}
            />
          </div>
        ) : loading ? (
          <TimedLoadingState
            label={t("flow.extractionExport.loading")}
            operationKey="document-extraction-export-load"
            framed={false}
            testId="document-extraction-export-loading"
          >
            <ListSkeleton rows={EXTRACTION_EXPORT_FORMATS.length} rowClassName="h-[6rem]" />
          </TimedLoadingState>
        ) : error ? (
          <Banner severity="warning" title={t("flow.extractionExport.loadError")}>
            {t("flow.extractionExport.loadErrorHint")}
          </Banner>
        ) : (
          <ul className="divide-y divide-border rounded-md border border-border bg-surface">
            {EXTRACTION_EXPORT_FORMATS.map(({ format, extension }) => {
              const label = t(FORMAT_LABEL_KEYS[format]);
              return (
                <li key={format} className="p-3" data-testid={`document-extraction-export-${format}`}>
                  <ContentActionBar
                    ariaLabel={t("flow.extractionExport.actions", { format: label })}
                    title={label}
                    description={t(FORMAT_DESCRIPTION_KEYS[format])}
                    meta={t("flow.extractionExport.fileExtension", { extension })}
                  >
                    {/* 押してから取得する（Markdown / HTML は画面が持っていない）。取得の間は loading。 */}
                    <CopyButton
                      size="sm"
                      text={() =>
                        api
                          .exportDocumentRecipeExtraction(documentId, recipeId, format)
                          .then((result) => result.content)
                      }
                      label={t("flow.extractionExport.copy")}
                      aria-label={t("flow.extractionExport.copyAria", { format: label })}
                      copiedMessage={t("flow.extractionExport.copied", { format: label })}
                      failedMessage={t("flow.extractionExport.copyFailed", { format: label })}
                      onCopied={() => setCopyFailedFormat(null)}
                      onCopyError={() => setCopyFailedFormat(format)}
                    />
                    <a
                      href={api.documentRecipeExtractionExportUrl(documentId, recipeId, format)}
                      download
                      aria-label={t("flow.extractionExport.downloadAria", { format: label })}
                      className={DOWNLOAD_LINK_CLASS}
                    >
                      <Download size={14} aria-hidden />
                      {t("flow.extractionExport.download")}
                    </a>
                  </ContentActionBar>
                  {copyFailedFormat === format ? (
                    <FormStatus
                      tone="danger"
                      className="mt-2"
                      message={t("flow.extractionExport.copyFailed", { format: label })}
                    />
                  ) : null}
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </section>
  );
}

/**
 * 「Chunk / Citation」タブの、保存済みの chunk の JSON のダウンロード（抽出エクスポートの Chunks 形式の置き換え。#561）。
 */
export function DocumentChunksJsonDownload({
  documentId,
  recipeId,
}: {
  documentId: string;
  recipeId: string;
}) {
  const format: DocumentExtractionExportFormat = "chunks";
  return (
    <ContentActionBar ariaLabel={t("flow.chunks.title")}>
      <a
        href={api.documentRecipeExtractionExportUrl(documentId, recipeId, format)}
        download
        aria-label={t("flow.chunks.downloadJsonAria")}
        className={DOWNLOAD_LINK_CLASS}
        data-testid="document-chunks-download-json"
      >
        <Download size={14} aria-hidden />
        {t("flow.chunks.downloadJson")}
      </a>
    </ContentActionBar>
  );
}
