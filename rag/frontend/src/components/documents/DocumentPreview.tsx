"use client";

import { useCallback, useMemo } from "react";

import {
  api,
  ApiError,
  apiErrorFromEnvelope,
  apiErrorFromFetchFailure,
  notifyResponseAuthStatus,
  type DocumentPreprocessArtifact,
  type SourcePreviewKind,
  type SourceProfile,
} from "@/lib/api";
import { appPath } from "@/lib/base-path";
import { t } from "@/lib/i18n";
import { useDocumentPreviewPages } from "@/lib/queries";
import { charsetFromContentType, decodeText } from "@/lib/text-decode";
import {
  DocumentPreview as SharedDocumentPreview,
  documentPreviewLabels,
  type BboxCoordinateMode,
  type BboxOverlayUnit,
  type BboxPageSize,
  type DocumentPreviewKind,
  type DocumentPreviewPagesState,
  type PreviewHighlight,
  type PreviewSizing,
  type PreviewViewerPage,
} from "@production-ready/ui";

type Kind = SourcePreviewKind;
type DocumentContentVariant = "original" | "prepared";

/** 原文のプレビューの文言（3 製品で同じ。再試行だけ RAG の共通の文言）。 */
const PREVIEW_LABELS = documentPreviewLabels({ retry: t("common.retry") });

function kindOf(fileName: string, sourceProfile?: SourceProfile | null): Kind {
  if (sourceProfile?.preview_kind) return sourceProfile.preview_kind;
  const ext = fileName.split(".").pop()?.toLowerCase() ?? "";
  if (["png", "jpg", "jpeg", "gif", "webp", "bmp"].includes(ext)) return "image";
  if (ext === "pdf") return "pdf";
  if (["txt", "csv", "md", "json", "log", "html", "htm", "eml"].includes(ext)) return "text";
  if (["doc", "docx", "ppt", "pptx", "xls", "xlsx"].includes(ext)) return "office";
  return "unsupported";
}

export function pdfPreviewUrl(url: string, focusPage?: number | null): string {
  const [baseUrl, existingHash = ""] = url.split("#", 2);
  const params = new URLSearchParams(existingHash);
  if (focusPage) params.set("page", String(focusPage));
  params.set("pagemode", "none");
  params.set("navpanes", "0");
  return `${baseUrl}#${params.toString()}`;
}

export function isPreparedPdfArtifact(
  artifact:
    | Pick<DocumentPreprocessArtifact, "object_storage_path" | "content_type" | "file_name">
    | null
    | undefined
): boolean {
  if (!artifact?.object_storage_path) return false;
  const contentType = artifact.content_type?.split(";", 1)[0].trim().toLowerCase();
  return contentType
    ? contentType === "application/pdf"
    : artifact.file_name.toLowerCase().endsWith(".pdf");
}

/**
 * 原本ファイルのプレビュー（画像 / PDF / テキスト）。表示は 3 製品で共通の `DocumentPreview`（#2389）で、
 * ここは RAG の文書の API（原本・処理後のファイル・ページの一覧と画像）をつなぐ。
 *
 * 画像と PDF（Office は変換済み PDF）はページ画像のビューアで表示し、回転・拡大縮小・フィット・パン・ページ送りと
 * bbox の強調を付ける（#349）。PDF のページ画像を描けないときは、ブラウザの PDF 表示（iframe）と位置の案内に戻す。
 * 高さは `sizing` で決める（`fill` は親に合わせる・`page` は 1 ページ全体が幅に合わせて入る。#559）。
 */
export function DocumentPreview({
  documentId,
  recipeId = null,
  fileName,
  variant = "original",
  sourceProfile = null,
  preparedArtifact = null,
  showFallbackDownload = true,
  focusPage = null,
  onPageChange,
  focusRequestKey = null,
  focusBbox = null,
  focusBboxMode = null,
  focusBboxUnit = null,
  focusPageSize = null,
  highlights = null,
  focusText = null,
  sizing = "fill",
  className,
}: {
  documentId: string;
  recipeId?: string | null;
  fileName: string;
  variant?: DocumentContentVariant;
  sourceProfile?: SourceProfile | null;
  preparedArtifact?: DocumentPreprocessArtifact | null;
  showFallbackDownload?: boolean;
  focusPage?: number | null;
  onPageChange?: (page: number) => void;
  focusRequestKey?: string | null;
  focusBbox?: number[] | null;
  focusBboxMode?: BboxCoordinateMode | null;
  focusBboxUnit?: BboxOverlayUnit | null;
  focusPageSize?: BboxPageSize | null;
  /** 強調する領域（要素の表示領域など）。省略時は focusBbox 1 つを強調する。 */
  highlights?: PreviewHighlight[] | null;
  /** テキストの原本で、引用した箇所（根拠の抜粋）へ移って強調する（#2512）。 */
  focusText?: string | null;
  sizing?: PreviewSizing;
  className?: string;
}) {
  const contentUrl = (
    options: { variant?: DocumentContentVariant; disposition?: "inline" | "attachment" } = {}
  ) =>
    recipeId
      ? api.documentRecipeContentUrl(documentId, recipeId, options)
      : api.documentContentUrl(documentId, options);
  const url = variant === "prepared" ? contentUrl({ variant }) : contentUrl();
  const downloadUrl = contentUrl({
    ...(variant === "prepared" ? { variant } : {}),
    disposition: "attachment",
  });
  const kind = kindOf(fileName, sourceProfile);
  // Office の原本（docx / pptx / xlsx 等）はブラウザで描けないので、preprocess が作った変換済み PDF があれば
  // それを表示する（抽出 bbox も変換済み PDF の座標系で合う）。
  const officeAsPdf = kind === "office" && isPreparedPdfArtifact(preparedArtifact) && variant !== "prepared";
  const pdfVariant: DocumentContentVariant = officeAsPdf ? "prepared" : variant;
  const pdfUrl = officeAsPdf ? contentUrl({ variant: "prepared" }) : url;
  const sharedKind: DocumentPreviewKind =
    kind === "pdf" || officeAsPdf
      ? "pdf"
      : kind === "image"
        ? "image"
        : kind === "text" || kind === "html" || kind === "email"
          ? "text"
          : "unsupported";
  const pages = useDocumentPdfPages(documentId, recipeId, pdfVariant, sharedKind === "pdf");
  const loadText = useCallback((signal: AbortSignal) => loadDocumentText(url, signal), [url]);

  return (
    <SharedDocumentPreview
      // 文書・レシピ・処理前後を切り替えたら、倍率・回転を初期化する。
      key={sharedKind === "pdf" ? pdfUrl : url}
      kind={sharedKind}
      fileName={fileName}
      imageUrl={url}
      pages={pages}
      iframeUrl={pdfPreviewUrl(pdfUrl, focusPage)}
      loadText={loadText}
      textKey={url}
      focusText={focusText}
      // 表示できない形式（Office の原本・処理後の JSONL など）は、形式によらず共通の案内の 1 文にする（#2679）。
      downloadUrl={showFallbackDownload ? downloadUrl : null}
      focusPage={focusPage}
      onPageChange={onPageChange}
      focusRequestKey={focusRequestKey}
      focusBbox={focusBbox}
      focusBboxMode={focusBboxMode}
      focusBboxUnit={focusBboxUnit}
      focusPageSize={focusPageSize}
      highlights={highlights}
      sizing={sizing}
      className={className}
      labels={PREVIEW_LABELS}
    />
  );
}

/** PDF のページの一覧（RAG の preview-pages API）→ 共通のプレビューのページの状態。 */
function useDocumentPdfPages(
  documentId: string,
  recipeId: string | null,
  variant: DocumentContentVariant,
  enabled: boolean
): DocumentPreviewPagesState {
  const pagesQuery = useDocumentPreviewPages(documentId, { recipeId, variant, enabled });
  const pages = useMemo<PreviewViewerPage[]>(
    () =>
      (pagesQuery.data?.pages ?? []).map((page) => ({
        pageNumber: page.page_number,
        points: { width: page.width, height: page.height },
        imageUrl: (dpi: number) =>
          api.documentPreviewPageImageUrl(documentId, page.page_number, { recipeId, variant, dpi }),
      })),
    [documentId, pagesQuery.data?.pages, recipeId, variant]
  );
  if (pagesQuery.isPending) return { status: "loading" };
  if (isMissingFileError(pagesQuery.error)) {
    // ファイルが保存先に無いときは、iframe に 404 の本文を出さず、理由と対処を出す（#1210）。
    return { status: "error", error: pagesQuery.error, onRetry: () => void pagesQuery.refetch() };
  }
  // ほかの失敗（描けないファイル・古い backend）は iframe の表示に戻す。
  if (pagesQuery.isError) return { status: "fallback" };
  return { status: "ready", pages };
}

/** 本文を読む（文字コードを判定して復号する。失敗は backend の理由と対処を持つ ApiError）。 */
async function loadDocumentText(url: string, signal: AbortSignal): Promise<string> {
  const request = { method: "GET", path: url.split("?")[0] };
  try {
    // url は api の URL を作る関数が base を付けて返す。送る所でも通す（何度通しても 1 回だけ付く。#1316）。
    const res = await fetch(appPath(url), { signal, credentials: "same-origin" });
    if (!res.ok) {
      // セッション切れはログインへ。経路の権限拒否以外の 403 はプレビュー内の失敗表示にとどめる（#224）。
      notifyResponseAuthStatus(res);
      // 原本が保存先に無いなど、backend が返した理由と対処を出す（#1210）。
      const envelope: unknown = await res.json().catch(() => null);
      throw apiErrorFromEnvelope(res.status, envelope, res.headers.get("X-Request-ID"));
    }
    const charset = charsetFromContentType(res.headers.get("Content-Type"));
    return decodeText(await res.arrayBuffer(), charset);
  } catch (error) {
    if (error instanceof ApiError || (error instanceof DOMException && error.name === "AbortError")) throw error;
    throw apiErrorFromFetchFailure(error, request) ?? error;
  }
}

/** 文書のファイルが保存先に無い（backend の `error_code`。#1210）。 */
const DOCUMENT_FILE_MISSING_CODE = "RAG_DOCUMENT_FILE_MISSING";

/** ページの一覧の失敗のうち、ファイルが保存先に無いことによる失敗か。ほかの失敗は iframe の表示に戻す。 */
function isMissingFileError(error: unknown): error is ApiError {
  return error instanceof ApiError && error.errorCode === DOCUMENT_FILE_MISSING_CODE;
}
