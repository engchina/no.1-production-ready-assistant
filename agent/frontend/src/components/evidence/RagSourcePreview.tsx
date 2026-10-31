import { useCallback, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  DocumentPreview,
  documentPreviewLabels,
  evidencePreviewFocus,
  type DocumentPreviewPagesState,
  type PreviewViewerPage,
} from "@production-ready/ui";

import { agentApi, type RunSourcePreview } from "@/lib/api";
import { t } from "@/lib/i18n";

/** 原文のプレビューの文言（RAG と同じ。再試行だけ Agent の共通の文言）。 */
const PREVIEW_LABELS = documentPreviewLabels({ retry: t("common.retry") });
/** 期限の何秒前に取り直すか（ページを送ったときに期限切れの URL を開かないように）。 */
const REFRESH_BEFORE_EXPIRY_SECONDS = 30;
const MIN_REFRESH_MS = 30_000;

/**
 * 根拠の原文のプレビュー（#2389）。RAG の根拠のパネルと同じ共通の `DocumentPreview`（ページの画像・ツールバー・
 * bbox の強調）で、強調の計算も同じ `evidencePreviewFocus`。
 *
 * データは Agent の backend から取る（画面を見ている利用者として RAG が作る、利用者・根拠・版に縛った短命の署名つきの
 * URL。RAG の画面用の Cookie の API は呼ばない）。URL は保存せず、パネルを開いている間は期限の前に取り直す。
 * 出せない根拠（権限が無い・ファイルが無い・資料の更新）は backend の理由と再試行を出す。
 */
export function RagSourcePreview({
  runId,
  documentId,
  chunkId,
  fileName,
  quote = null,
  open,
}: {
  runId: string;
  documentId: string;
  chunkId: string;
  /** 根拠の文書名（プレビューを取るまでの見出しと読み上げ）。 */
  fileName: string;
  /** 根拠の抜粋。テキストの原文では、その箇所へ移って強調する（#2512）。 */
  quote?: string | null;
  /** パネルが開いているか（閉じている間は取らない・取り直さない）。 */
  open: boolean;
}) {
  const preview = useQuery({
    queryKey: ["run-source-preview", runId, documentId, chunkId],
    queryFn: () => agentApi.getRunSourcePreview(runId, documentId, chunkId),
    enabled: open,
    // URL は短命（5 分）なので cache しない。開いている間は期限の前に取り直す。
    staleTime: 0,
    gcTime: 0,
    retry: false,
    refetchOnWindowFocus: false,
    refetchInterval: (query) => {
      const data = query.state.data;
      if (!open || !data) return false;
      return Math.max((data.expires_in_seconds - REFRESH_BEFORE_EXPIRY_SECONDS) * 1000, MIN_REFRESH_MS);
    },
  });
  const data = preview.data;
  const focus = useMemo(() => evidencePreviewFocus(data?.highlight_metadata ?? null), [data?.highlight_metadata]);
  const pages = usePreviewPages(data);
  const contentUrl = data?.content_url ?? null;
  const loadText = useCallback((signal: AbortSignal) => loadPreviewText(contentUrl ?? "", signal), [contentUrl]);

  if (preview.isError) {
    return (
      <DocumentPreview
        kind="pdf"
        fileName={fileName}
        pages={{ status: "error", error: preview.error, onRetry: () => void preview.refetch() }}
        labels={PREVIEW_LABELS}
        className="min-h-0 flex-1"
      />
    );
  }
  if (!data) {
    return (
      <DocumentPreview
        kind="pdf"
        fileName={fileName}
        pages={{ status: "loading" }}
        labels={PREVIEW_LABELS}
        className="min-h-0 flex-1"
      />
    );
  }
  return (
    <DocumentPreview
      kind={data.kind}
      fileName={data.file_name || fileName}
      imageUrl={data.content_url}
      pages={pages}
      // ページの画像を描けない PDF は、原文の URL をブラウザの PDF 表示で開く。
      iframeUrl={data.content_url}
      loadText={loadText}
      // 期限の前に取り直しても、本文は読み直さない（同じ根拠の同じ本文）。
      textKey={`${documentId}:${chunkId}`}
      focusText={quote}
      unsupportedMessage={t("evidence.previewUnsupported")}
      downloadUrl={`${data.content_url}?disposition=attachment`}
      focusPage={focus.focusPage}
      focusBbox={focus.focusBbox}
      focusBboxMode={focus.focusBboxMode}
      focusBboxUnit={focus.focusBboxUnit}
      focusPageSize={focus.focusPageSize}
      highlights={focus.highlights}
      labels={PREVIEW_LABELS}
      className="min-h-0 flex-1"
    />
  );
}

/** プレビューのページの一覧 → 共通のプレビューのページの状態（画像の URL はひな形に番号と dpi を入れる）。 */
function usePreviewPages(data: RunSourcePreview | undefined): DocumentPreviewPagesState {
  return useMemo(() => {
    const template = data?.page_url_template;
    if (!data || !template || data.pages.length === 0) return { status: "fallback" };
    const pages: PreviewViewerPage[] = data.pages.map((page) => ({
      pageNumber: page.page_number,
      points: { width: page.width, height: page.height },
      imageUrl: (dpi: number) =>
        `${template.replace("{page}", String(page.page_number))}${dpi ? `?dpi=${dpi}` : ""}`,
    }));
    return { status: "ready", pages };
  }, [data]);
}

/** 原文の本文を読む（RAG の署名つきの URL。Cookie と Referer を送らない）。 */
async function loadPreviewText(url: string, signal: AbortSignal): Promise<string> {
  const response = await fetch(url, { signal, credentials: "omit", referrerPolicy: "no-referrer" });
  if (!response.ok) {
    const envelope: unknown = await response.json().catch(() => null);
    const messages =
      envelope && typeof envelope === "object" && "error_messages" in envelope
        ? (envelope as { error_messages?: unknown }).error_messages
        : null;
    const message = Array.isArray(messages) && typeof messages[0] === "string" ? messages[0] : null;
    throw new Error(message ?? t("evidence.previewTextFailed"));
  }
  const charset = /charset=([^;]+)/i.exec(response.headers.get("Content-Type") ?? "")?.[1]?.trim() || "utf-8";
  const buffer = await response.arrayBuffer();
  try {
    return new TextDecoder(charset).decode(buffer);
  } catch {
    return new TextDecoder("utf-8").decode(buffer);
  }
}
