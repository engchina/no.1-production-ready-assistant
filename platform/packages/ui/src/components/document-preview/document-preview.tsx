import { Download, FileQuestion, LocateFixed } from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";

import {
  type BboxCoordinateMode,
  type BboxOverlayRect,
  type BboxOverlayUnit,
  type BboxPageSize,
  type PreviewHighlight,
  bboxPageAspectRatio,
  buildPreviewHighlights,
  formatBboxPercent,
  normalizeBboxForPreview,
} from "../../lib/bbox";
import { resolveContentLang } from "../../lib/content-lang";
import { excerptPlainText } from "../../lib/excerpt-text";
import { findTextQuote } from "../../lib/text-quote";
import { INFORMATION_TABLE_FOCUS_CLASS } from "../../lib/list-density";
import { cn } from "../../lib/utils";
import { ApiErrorState } from "../feedback/api-error-banner";
import { TimedLoadingState } from "../feedback/processing-state";
import { Button, buttonVariants } from "../ui/button";
import { Skeleton } from "../ui/skeleton";
import { DEFAULT_DOCUMENT_PREVIEW_LABELS, type DocumentPreviewLabels } from "./labels";
import { bboxOverlayStyle, PreviewViewer, type PreviewSizing, type PreviewViewerPage } from "./preview-viewer";

/**
 * 原文のプレビューの種類。`pdf` はページの画像のビューア（Office は製品が変換済みの PDF を渡す）、`image` は画像 1 枚、
 * `text` は本文（txt・csv・md・json・html・eml など）、`unsupported` は表示できない形式の案内。
 */
export type DocumentPreviewKind = "pdf" | "image" | "text" | "unsupported";

/**
 * PDF のページの一覧の状態（製品が自分の API で取って渡す。#2389）。
 * - `loading`: 取得中（Skeleton）。
 * - `ready`: ページの画像のビューア。0 ページなら `fallback` と同じ扱い。
 * - `fallback`: ページの画像を描けない（描けないファイル・古い backend）。`iframeUrl` があればブラウザの PDF 表示。
 * - `error`: 理由を出す失敗（ファイルが保存先に無い・権限が無い・期限切れなど）。`onRetry` で取り直す。
 */
export type DocumentPreviewPagesState =
  | { status: "loading" }
  | { status: "ready"; pages: PreviewViewerPage[] }
  | { status: "fallback" }
  | { status: "error"; error: unknown; onRetry?: () => void };

export interface DocumentPreviewFocus {
  focusRequestKey?: string | null;
  focusPage?: number | null;
  focusBbox?: number[] | null;
  focusBboxMode?: BboxCoordinateMode | null;
  focusBboxUnit?: BboxOverlayUnit | null;
  focusPageSize?: BboxPageSize | null;
}

export interface DocumentPreviewProps extends DocumentPreviewFocus {
  kind: DocumentPreviewKind;
  /** ページ画像のビューアで表示中の実ページを通知する。 */
  onPageChange?: (page: number) => void;
  fileName: string;
  /** `image`: 画像の URL。 */
  imageUrl?: string | null;
  /** `pdf`: ページの一覧の状態。 */
  pages?: DocumentPreviewPagesState;
  /** `pdf`: ページの画像を描けないときに開くブラウザの PDF 表示の URL。 */
  iframeUrl?: string | null;
  /**
   * `text`: 本文を読む。失敗は throw する（`ApiErrorState` が理由と対処を出す）。`textKey` が変わると読み直す。
   */
  loadText?: (signal: AbortSignal) => Promise<string>;
  textKey?: string;
  /**
   * `text`: 引用した箇所（根拠の抜粋の本文）。本文の中で見つかれば、そこへスクロールして強調する（PDF の bbox の強調と
   * 同じ考え方。#2512）。照合は文字と数字だけを比べる汎用の仕組みで、見つからなければ先頭のまま。
   */
  focusText?: string | null;
  /**
   * `text`: 本文の言語（BCP 47。#3335。`resolveContentLang`）。取込で分かっていれば渡す。渡さなければ本文から判定して
   * 本文の領域に `lang` を付け（漢字を内容の言語の字形で描く）、`null` なら付けない。
   */
  textLang?: string | null;
  /** `unsupported` の案内（既定は {@link DocumentPreviewLabels.unsupported}）。 */
  unsupportedMessage?: string;
  /** `unsupported` で出すダウンロードの URL。無ければボタンを出さない。 */
  downloadUrl?: string | null;
  /** 強調する領域（要素の表示領域など）。省略時は focusBbox 1 つを強調する。 */
  highlights?: PreviewHighlight[] | null;
  /**
   * 高さの決め方（#559）。`fill`（既定）は親に合わせる（根拠のパネル）。`page` は 1 ページ全体が幅に合わせて入る高さ
   * （文書の詳細）。ページの寸法が分からない表示（読み込み中・iframe・テキスト）は A4 縦の縦横比で領域を取る。
   */
  sizing?: PreviewSizing;
  className?: string;
  labels?: DocumentPreviewLabels;
}

/**
 * 原文のプレビュー（画像 / PDF / テキスト。RAG の文書の詳細・根拠のパネル、Agent の根拠のパネルで同じ部品。#349・#2389）。
 *
 * 画像と PDF はページ画像のビューア（{@link PreviewViewer}）で表示し、回転・拡大縮小・フィット・パン・ページ送りと
 * bbox の強調を付ける。データの取り方（URL・ページの一覧・本文）は製品が props で渡し、部品は製品の API を知らない。
 */
export function DocumentPreview({
  kind,
  fileName,
  imageUrl = null,
  pages = { status: "fallback" },
  iframeUrl = null,
  loadText,
  textKey,
  focusText = null,
  textLang,
  unsupportedMessage,
  downloadUrl = null,
  focusPage = null,
  onPageChange,
  focusRequestKey = null,
  focusBbox = null,
  focusBboxMode = null,
  focusBboxUnit = null,
  focusPageSize = null,
  highlights = null,
  sizing = "fill",
  className,
  labels = DEFAULT_DOCUMENT_PREVIEW_LABELS,
}: DocumentPreviewProps) {
  const focus = { focusBbox, focusBboxMode, focusBboxUnit, focusPage, focusPageSize };
  const viewerClassName = cn(sizing === "page" ? "flex-auto" : "h-full min-h-80", className);
  const effectiveHighlights =
    highlights ?? buildPreviewHighlights({ focusPage, focusBbox, focusBboxMode, focusBboxUnit, focusPageSize });

  if (kind === "image" && imageUrl) {
    return (
      <PreviewViewer
        key={imageUrl}
        onPageChange={onPageChange}
        focusRequestKey={focusRequestKey}
        kind="image"
        sizing={sizing}
        className={viewerClassName}
        fileName={fileName}
        pages={[{ pageNumber: 1, imageUrl: () => imageUrl }]}
        focusPage={1}
        // 画像は 1 ページとして扱う（ページ番号の無い・1 以外の値でも同じ画像に重ねる）。
        highlights={effectiveHighlights.map((highlight) => ({ ...highlight, page: null }))}
        labels={labels}
      />
    );
  }

  if (kind === "pdf") {
    if (pages.status === "loading") {
      return (
        <TimedLoadingState
          label={labels.loading}
          placement="panel"
          testId="preview-loading"
          className={cn(sizing === "page" ? "flex-auto" : "h-full min-h-80", "grid-rows-[auto_minmax(0,1fr)]", className)}
        >
          {/* `page` はページの寸法が届く前なので A4 縦で領域を取る（届いた後のビューアとほぼ同じ高さ。#559）。 */}
          <Skeleton className={cn("w-full", sizing === "page" ? PAGE_SIZED_FRAME_CLASS : "h-full min-h-60")} />
        </TimedLoadingState>
      );
    }
    if (pages.status === "error") {
      // 理由のある失敗（ファイルが保存先に無いなど。#1210）は、iframe に本文を出さず理由と対処を出す。
      return (
        <ApiErrorState error={pages.error} fallback={labels.fetchError} onRetry={pages.onRetry} retryLabel={labels.retry} />
      );
    }
    if (pages.status === "ready" && pages.pages.length > 0) {
      return (
        <PreviewViewer
          onPageChange={onPageChange}
        focusRequestKey={focusRequestKey}
          kind="pdf"
          sizing={sizing}
          className={viewerClassName}
          fileName={fileName}
          pages={pages.pages}
          focusPage={focusPage ?? null}
          highlights={effectiveHighlights}
          labels={labels}
        />
      );
    }
    if (iframeUrl) {
      return (
        <PreviewFrame className={className} sizing={sizing} labels={labels} {...focus}>
          <iframe
            src={iframeUrl}
            title={fileName}
            className="h-full min-h-80 w-full rounded-md border border-border bg-surface"
          />
        </PreviewFrame>
      );
    }
    return <UnsupportedPreview fileName={fileName} url={downloadUrl} message={labels.fetchError} labels={labels} />;
  }

  if (kind === "text" && loadText) {
    return (
      <PreviewFrame className={className} sizing={sizing} labels={labels} {...focus}>
        <TextPreview
          key={textKey}
          loadText={loadText}
          fileName={fileName}
          focusText={focusText}
          textLang={textLang}
          labels={labels}
        />
      </PreviewFrame>
    );
  }

  return (
    <UnsupportedPreview
      fileName={fileName}
      url={downloadUrl}
      message={unsupportedMessage ?? labels.unsupported}
      labels={labels}
    />
  );
}

/**
 * ページの寸法が分からない表示（読み込み中・iframe の PDF・テキスト）の `page` の高さ（#559）。
 * A4 縦（210 : 297）の縦横比で幅から高さを決め、上限はページ画像のビューアと同じ 2 画面分にする。
 */
const PAGE_SIZED_FRAME_CLASS = "aspect-[210/297] max-h-[calc(200dvh-10rem)]";

/** ページ画像で表示できないとき（iframe の PDF・テキスト）に、位置の案内を上に添える。 */
function PreviewFrame({
  children,
  focusBbox,
  focusBboxMode = null,
  focusBboxUnit = null,
  focusPage = null,
  focusPageSize = null,
  sizing = "fill",
  className,
  labels,
}: DocumentPreviewFocus & {
  children: ReactNode;
  sizing?: PreviewSizing;
  className?: string;
  labels: DocumentPreviewLabels;
}) {
  const overlayRect = normalizeBboxForPreview(focusBbox, focusPageSize, focusBboxMode, focusBboxUnit);
  return (
    <div className={cn("flex flex-col gap-2", sizing === "page" ? "flex-auto" : "h-full min-h-0", className)}>
      {focusBbox && overlayRect ? (
        <BboxLocator overlayRect={overlayRect} focusPage={focusPage} focusPageSize={focusPageSize} labels={labels} />
      ) : null}
      {overlayRect ? (
        <BboxPreviewOverlay overlayRect={overlayRect} focusPage={focusPage} focusPageSize={focusPageSize} labels={labels} />
      ) : null}
      {sizing === "page" ? (
        // A4 縦の縦横比で領域を取り、中身（iframe・テキスト）はその中いっぱいに置く（テキストは中でスクロールする）。
        <div className={cn("relative w-full flex-auto", PAGE_SIZED_FRAME_CLASS)}>
          <div className="absolute inset-0 flex flex-col">{children}</div>
        </div>
      ) : (
        <div className="relative min-h-0 flex-1">{children}</div>
      )}
    </div>
  );
}

function BboxPreviewOverlay({
  overlayRect,
  focusPage,
  focusPageSize,
  labels,
}: {
  overlayRect: BboxOverlayRect;
  focusPage: number | null;
  focusPageSize: BboxPageSize | null;
  labels: DocumentPreviewLabels;
}) {
  return (
    <div
      aria-label={labels.bboxPreviewLabel(focusPage ?? "—")}
      data-testid="bbox-preview-page"
      className="relative mx-auto max-h-72 w-full max-w-56 overflow-hidden rounded-md border border-accent-emphasis bg-surface shadow-sm"
      role="img"
      style={{ aspectRatio: bboxPageAspectRatio(focusPageSize) }}
    >
      <span
        data-surface-tint="accent"
        aria-hidden
        data-bbox-mode={overlayRect.coordinateMode}
        data-bbox-unit={overlayRect.unit}
        data-testid="bbox-preview-overlay"
        className="pointer-events-none absolute rounded-sm border-2 border-accent-emphasis bg-accent-muted ring-1 ring-surface/90"
        style={bboxOverlayStyle(overlayRect)}
      />
    </div>
  );
}

function BboxLocator({
  overlayRect,
  focusPage,
  focusPageSize,
  labels,
}: {
  overlayRect: BboxOverlayRect;
  focusPage: number | null;
  focusPageSize: BboxPageSize | null;
  labels: DocumentPreviewLabels;
}) {
  return (
    <div
      data-surface-tint="info"
      role="status"
      aria-live="polite"
      className="rounded-md border border-info-border bg-info-subtle p-3 text-info-fg"
    >
      <div className="grid grid-cols-1 items-center gap-3 sm:grid-cols-[minmax(0,1fr)_4rem]">
        <p className="tnum min-w-0 break-words text-xs">
          {labels.bboxFocus({
            page: focusPage ?? "—",
            x: formatBboxPercent(overlayRect.leftPercent),
            y: formatBboxPercent(overlayRect.topPercent),
            width: formatBboxPercent(overlayRect.widthPercent),
            height: formatBboxPercent(overlayRect.heightPercent),
          })}
        </p>
        <div
          aria-label={labels.bboxMapLabel(focusPage ?? "—")}
          data-testid="bbox-page-map"
          className="relative mx-auto w-16 overflow-hidden rounded-sm border border-info-border bg-surface-sunken shadow-sm"
          style={{ aspectRatio: bboxPageAspectRatio(focusPageSize) }}
        >
          <span
            data-surface-tint="accent"
            aria-hidden
            data-bbox-mode={overlayRect.coordinateMode}
            data-bbox-unit={overlayRect.unit}
            data-testid="bbox-overlay"
            className="pointer-events-none absolute rounded-sm border-2 border-accent-emphasis bg-accent-muted"
            style={bboxOverlayStyle(overlayRect)}
          />
        </div>
      </div>
    </div>
  );
}

/**
 * 本文の表示（読み込み・失敗と再試行・中でスクロール）。読み方は製品の `loadText`。`focusText`（引用した箇所）が
 * 本文の中で見つかれば強調し、本文の領域の中だけをその位置へスクロールする（パネル全体は動かさない。#2512）。
 */
function TextPreview({
  loadText,
  fileName,
  focusText,
  textLang,
  labels,
}: {
  loadText: (signal: AbortSignal) => Promise<string>;
  fileName: string;
  focusText: string | null;
  textLang: string | null | undefined;
  labels: DocumentPreviewLabels;
}) {
  // 取得結果は試行ごとに持つ（再試行の直後は前の結果を使わず、読込中として扱う）。
  const [attempt, setAttempt] = useState(0);
  const [result, setResult] = useState<{ attempt: number; text: string | null; error: unknown } | null>(null);
  const current = result?.attempt === attempt ? result : null;

  useEffect(() => {
    const controller = new AbortController();
    loadText(controller.signal)
      .then((text) => setResult({ attempt, text, error: null }))
      .catch((error: unknown) => {
        if (!(error instanceof DOMException && error.name === "AbortError")) {
          setResult({ attempt, text: null, error });
        }
      });
    return () => controller.abort();
    // loadText は描画ごとに作り直されることがあるので、読み直しは textKey（key）と再試行だけで決める。
  }, [attempt]);

  if (current?.error != null) {
    return (
      <ApiErrorState
        error={current.error}
        fallback={labels.fetchError}
        onRetry={() => setAttempt((value) => value + 1)}
        retryLabel={labels.retry}
      />
    );
  }
  if (current?.text == null) {
    return (
      <TimedLoadingState
        label={labels.textLoading}
        operationKey="preview-text-load"
        framed={false}
        testId="preview-text-loading"
      >
        <Skeleton className="h-40 w-full" />
      </TimedLoadingState>
    );
  }
  return (
    <TextPreviewBody text={current.text} fileName={fileName} focusText={focusText} textLang={textLang} labels={labels} />
  );
}

function TextPreviewBody({
  text,
  fileName,
  focusText,
  textLang,
  labels,
}: {
  text: string;
  fileName: string;
  focusText: string | null;
  textLang: string | null | undefined;
  labels: DocumentPreviewLabels;
}) {
  const contentLang = useMemo(() => resolveContentLang(textLang, text), [textLang, text]);
  // 抜粋に入る文書の表（HTML の `<table>`）は、画面の抜粋と同じく文字にしてから照合する。
  const range = useMemo(() => (focusText ? findTextQuote(text, excerptPlainText(focusText)) : null), [text, focusText]);
  const scrollRef = useRef<HTMLPreElement>(null);
  const markRef = useRef<HTMLElement>(null);
  const scrollToQuote = useCallback(() => {
    const region = scrollRef.current;
    const mark = markRef.current;
    if (!region || !mark) return;
    // scrollIntoView はパネル全体も動かすので、本文の領域の中だけを動かす（強調の上に少し前の行を残す）。
    region.scrollTop = Math.max(0, mark.offsetTop - region.clientHeight / 6);
  }, []);
  useLayoutEffect(() => {
    if (range) scrollToQuote();
  }, [range, scrollToQuote]);

  return (
    <div className="flex h-full min-h-0 flex-col gap-2">
      {range ? (
        <div className="flex shrink-0 items-center justify-between gap-2" data-testid="preview-text-highlight">
          <p role="status" className="min-w-0 text-xs text-fg-muted">
            {labels.textHighlightStatus}
          </p>
          <Button type="button" variant="ghost" size="sm" icon={LocateFixed} onClick={scrollToQuote}>
            {labels.focusHighlight}
          </Button>
        </div>
      ) : null}
      <pre
        ref={scrollRef}
        role="region"
        aria-label={labels.region(fileName)}
        // 本文は中でスクロールするので、Tab で届き矢印キーで動かせるようにする。
        tabIndex={0}
        className={cn(
          "relative min-h-40 flex-1 overflow-auto rounded-md border border-border bg-surface p-4 text-sm leading-relaxed whitespace-pre-wrap break-words text-fg",
          INFORMATION_TABLE_FOCUS_CLASS
        )}
        data-testid="preview-text"
      >
        {/* 本文だけを言語で包む（領域の読み上げの名前は画面の言語。#3349）。 */}
        <span lang={contentLang} data-testid="preview-text-body">
          {range ? (
            <>
              {text.slice(0, range.start)}
              <mark
              ref={markRef}
              data-surface-tint="accent"
              className="rounded-sm bg-accent-muted text-fg underline decoration-accent-emphasis decoration-2 underline-offset-2 box-decoration-clone"
                data-testid="preview-text-quote"
              >
                {text.slice(range.start, range.end)}
              </mark>
              {text.slice(range.end)}
            </>
          ) : (
            text
          )}
        </span>
      </pre>
    </div>
  );
}

function UnsupportedPreview({
  fileName,
  url,
  message,
  labels,
}: {
  fileName: string;
  url: string | null;
  message: string;
  labels: DocumentPreviewLabels;
}) {
  return (
    <div className="flex min-h-40 flex-col items-center justify-center gap-3 rounded-md border border-border bg-surface p-4 text-center text-fg-muted">
      <FileQuestion size={24} aria-hidden />
      <p className="text-sm">{message}</p>
      {url ? (
        <a href={url} download={fileName} className={buttonVariants({ variant: "secondary", size: "md" })}>
          <Download size={16} aria-hidden />
          {labels.download}
        </a>
      ) : null}
    </div>
  );
}
