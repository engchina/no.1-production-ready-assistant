import {
  ChevronLeft,
  ChevronRight,
  LocateFixed,
  Maximize,
  MoveHorizontal,
  RotateCcw,
  RotateCw,
  ZoomIn,
  ZoomOut,
} from "lucide-react";
import {
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  type RefObject,
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import {
  type BboxOverlayRect,
  type BboxPageSize,
  type PreviewHighlight,
  highlightPages,
  highlightRectsForPage,
} from "../../lib/bbox";
import { MEASURED_SIZE_TOLERANCE_PX, stabilizeMeasuredBox } from "../../lib/measured-size";
import {
  type PreviewFitMode,
  PREVIEW_MAX_ZOOM,
  PREVIEW_MIN_ZOOM,
  anchoredScroll,
  clampZoom,
  normalizeViewRotation,
  previewDpiFor,
  previewHeightAspect,
  previewKeyAction,
  previewLayout,
  steppedZoom,
} from "../../lib/preview-viewer";
import { cn } from "../../lib/utils";
import { ProcessingIndicator } from "../feedback/processing-state";
import { Button } from "../ui/button";
import { Skeleton } from "../ui/skeleton";
import { DEFAULT_DOCUMENT_PREVIEW_LABELS, type DocumentPreviewLabels } from "./labels";

export type PreviewViewerPage = {
  pageNumber: number;
  /** ページ画像の URL。PDF は倍率に合わせた dpi を受け取る。 */
  imageUrl: (dpi: number) => string;
  /** 表示寸法（pt）。PDF は preview-pages API の値。画像は読み込み後の実寸を使う。 */
  points?: { width: number; height: number } | null;
};

type Anchor = {
  pointX: number;
  pointY: number;
  scrollLeft: number;
  scrollTop: number;
  previousWidth: number;
  previousHeight: number;
};

/**
 * プレビューの高さの決め方（#559）。
 *
 * - `fill`: 親が与える高さに合わせる（引用のダイアログなど。ページは内部でスクロールする）。
 * - `page`: 表示領域（ビューポート）の高さを、幅と文書のページの縦横比から決める。幅に合わせたときに
 *   1 ページ全体が縦スクロールなしで入る（文書詳細）。ツールバーと強調の状態の行は、その上に足される。
 */
export type PreviewSizing = "fill" | "page";

/**
 * `page` のビューポートの高さ（#559）。CSS だけで決める（幅が変わっても JS の計測を待たずに追従し、
 * ResizeObserver との往復で高さが揺れない）。
 *
 * - `100cqw` はビューア（`@container`）の内側の幅 = ビューポートの幅。ステージの左右・上下の余白（p-3 = 0.75rem × 2）を
 *   引いて縦横比で割り、上下の余白を足し戻す。さらに 0.25rem の遊びを足し、幅の端数の丸めで 1px はみ出して
 *   スクロールバーが出るのを防ぐ（余白はページの上下に等分されるので見えない）。
 * - 下限 15rem: 横に極端に長いページ（パノラマ画像など）でも、ツールバーの操作と強調が見える高さを残す。
 * - 上限 2 画面分（200dvh からペインの見出し・タブ・ツールバー・状態の行のおよそ 10rem を引く）: レシートのような
 *   極端に縦長の画像でペインが際限なく伸びないようにする。上限にかかったときだけ、幅に合わせた表示で内部スクロールになる。
 */
const PAGE_SIZED_VIEWPORT_CLASS =
  "flex-auto h-[calc((100cqw-1.5rem)/var(--preview-page-aspect)+1.75rem)] min-h-60 max-h-[calc(200dvh-10rem)]";

/**
 * 文書のプレビュー（ページ画像）に、回転・拡大縮小・フィット・パン・ページ送りと bbox の強調を付ける（#349）。
 * RAG の文書の詳細・根拠のパネルと Agent の根拠のパネルで同じ部品（#2389）。ページの画像の URL は製品が渡す。
 *
 * - 画像と強調を同じ層に置いて一緒に回転・拡大するので、強調は常に対応する位置に重なる。
 * - 高さは `sizing` で決める（`fill` は親に合わせ、`page` は 1 ページ全体が入る高さ。#559）。
 *   どちらも拡大するとページは内部でスクロールし、ビューアの高さは変わらない。
 * - 倍率・回転は表示中の間だけ保持し、文書・レシピ・処理前後を切り替えると親の key で初期化する。
 */
export function PreviewViewer({
  pages,
  fileName,
  focusPage = null,
  onPageChange,
  focusRequestKey = null,
  highlights = [],
  kind,
  sizing = "fill",
  className,
  labels = DEFAULT_DOCUMENT_PREVIEW_LABELS,
}: {
  pages: PreviewViewerPage[];
  fileName: string;
  focusPage?: number | null;
  /** 同じページへの再移動を識別する任意のキー。 */
  focusRequestKey?: string | null;
  /** 表示中の実ページ番号を通知する。初期表示・ページ送り・出典への移動を含む。 */
  onPageChange?: (page: number) => void;
  highlights?: PreviewHighlight[];
  /** image は画像ファイルそのもの（強調の基準寸法が無いときは画像の実寸 px を使う）。 */
  kind: "image" | "pdf";
  sizing?: PreviewSizing;
  className?: string;
  labels?: DocumentPreviewLabels;
}) {
  const hintId = useId();
  const viewportRef = useRef<HTMLDivElement>(null);
  const stageRef = useRef<HTMLDivElement>(null);
  const [viewportSize, setViewportSize] = useState<{ width: number; height: number } | null>(
    null
  );
  const [mode, setMode] = useState<PreviewFitMode>("fit-width");
  const [zoomPercent, setZoomPercent] = useState(100);
  const [rotations, setRotations] = useState<Record<number, number>>({});
  const [naturalSizes, setNaturalSizes] = useState<Record<number, BboxPageSize>>({});
  const [loadedSources, setLoadedSources] = useState<Record<string, boolean>>({});
  const [failedSources, setFailedSources] = useState<Record<string, boolean>>({});
  const [dpiByPage, setDpiByPage] = useState<Record<number, number>>({});
  const [dragging, setDragging] = useState(false);
  const dragRef = useRef<{ x: number; y: number; left: number; top: number; id: number } | null>(
    null
  );
  const anchorRef = useRef<Anchor | null>(null);
  // 強調の位置へのスクロールの要求（番号を増やして要求し、処理済みの番号は ref に持つ）。
  const [scrollRequest, setScrollRequest] = useState(highlights.length > 0 ? 1 : 0);
  const handledScrollRef = useRef(0);

  const pageNumbers = useMemo(() => pages.map((page) => page.pageNumber), [pages]);
  const highlightPageNumbers = useMemo(() => highlightPages(highlights), [highlights]);
  const highlightKey = useMemo(
    () => `${focusRequestKey ?? ""}|${focusPage ?? ""}|${highlights.map((highlight) => highlight.key).join("|")}`,
    [focusRequestKey, focusPage, highlights]
  );
  const initialPage = preferredPage(focusPage, highlightPageNumbers, pageNumbers);
  const [currentPage, setCurrentPage] = useState<number>(initialPage);
  // フォーカス（chunk・要素・引用）が変わったら、そのページへ移って強調の位置までスクロールする。
  const [appliedHighlightKey, setAppliedHighlightKey] = useState(highlightKey);
  if (appliedHighlightKey !== highlightKey) {
    setAppliedHighlightKey(highlightKey);
    setCurrentPage(initialPage);
    setScrollRequest((current) => current + 1);
  }
  const pageIndex = Math.max(0, pageNumbers.indexOf(currentPage));
  const page = pages[pageIndex] ?? pages[0];
  const pageNumber = page?.pageNumber ?? 1;
  useEffect(() => {
    if (pages.length) onPageChange?.(pageNumber);
  }, [pageNumber, pages.length, onPageChange]);
  const rotation = rotations[pageNumber] ?? 0;
  const natural = naturalSizes[pageNumber] ?? null;
  const points = page?.points ?? null;
  const aspect =
    points && points.width > 0 && points.height > 0
      ? points.width / points.height
      : natural?.width && natural?.height
        ? natural.width / natural.height
        : null;

  // `page` の高さに使う縦横比。文書で最も縦長のページ（画像は読み込み後の実寸）。分かるまでは A4 縦（#559）。
  const heightAspect = useMemo(
    () => previewHeightAspect(pages.map((item) => item.points ?? naturalSizes[item.pageNumber])),
    [naturalSizes, pages]
  );

  const stagePadding = useStagePadding(stageRef);
  const layout = viewportSize
    ? previewLayout({
        aspect,
        rotation,
        mode,
        zoomPercent,
        // 拡大 125% などで clientWidth / clientHeight が実寸より大きく丸められても、ページがはみ出して
        // スクロールバーが出入りしない（幅が変わってフィットの計算と往復しない）よう 1px の余裕を残す。
        // フィットの表示でスクロールバーが出ないので、幅の予約（scrollbar-gutter）はしない（ページの幅を
        // 狭めない。#1222）。
        availableWidth: viewportSize.width - stagePadding.x - MEASURED_SIZE_TOLERANCE_PX,
        availableHeight: viewportSize.height - stagePadding.y - MEASURED_SIZE_TOLERANCE_PX,
      })
    : null;

  const requestedDpi =
    kind === "pdf" && layout
      ? previewDpiFor(layout.shellWidth, points?.width, devicePixelRatioValue())
      : 0;
  const dpi = Math.max(dpiByPage[pageNumber] ?? 0, requestedDpi);
  if (kind === "pdf" && requestedDpi > (dpiByPage[pageNumber] ?? 0)) {
    // 一度上げた解像度は下げない（縮小のたびに取り直さない）。
    setDpiByPage((current) => ({ ...current, [pageNumber]: requestedDpi }));
  }
  const src = page ? page.imageUrl(dpi) : "";
  const loaded = Boolean(loadedSources[`${pageNumber}`]);
  const failed = Boolean(failedSources[src]);

  const rects = useMemo(
    () =>
      highlightRectsForPage(highlights, pageNumber, {
        pagePoints: (target) => pages.find((item) => item.pageNumber === target)?.points ?? null,
        fallbackPageSize: kind === "image" ? natural : null,
      }),
    [highlights, kind, natural, pageNumber, pages]
  );

  // ビューポートの実寸（フィットの計算に使う）。
  useLayoutEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    // 測った大きさの 1px の揺れは小さい方に寄せて書き戻さない（ページはビューポートの内側に置くため、
    // 大きい方に寄せるとはみ出す。#1222）。
    const measure = () =>
      setViewportSize((current) =>
        stabilizeMeasuredBox(
          current,
          { width: viewport.clientWidth, height: viewport.clientHeight },
          { prefer: "smaller" }
        )
      );
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(viewport);
    return () => observer.disconnect();
  }, []);

  // 拡大縮小の前後で、基準の点（ボタン・キーは中央、ホイールはカーソル位置）を同じ場所に保つ。
  const frameWidth = layout?.frameWidth ?? 0;
  const frameHeight = layout?.frameHeight ?? 0;
  useLayoutEffect(() => {
    const viewport = viewportRef.current;
    const anchor = anchorRef.current;
    if (!viewport || !anchor) return;
    anchorRef.current = null;
    const next = anchoredScroll({
      ...anchor,
      nextWidth: viewport.scrollWidth,
      nextHeight: viewport.scrollHeight,
    });
    viewport.scrollLeft = next.left;
    viewport.scrollTop = next.top;
  }, [frameWidth, frameHeight]);

  const scrollToHighlight = useCallback((options: { revealInPage?: boolean } = {}) => {
    const viewport = viewportRef.current;
    const target = viewport?.querySelector<HTMLElement>("[data-highlight-tone='primary']");
    if (!viewport || !target) return;
    const viewportBox = viewport.getBoundingClientRect();
    const targetBox = target.getBoundingClientRect();
    // 強調の中心をビューポートの中央付近へ（rag_poc の scrollPaneToElement と同じ考え方）。
    viewport.scrollLeft += targetBox.left + targetBox.width / 2 - (viewportBox.left + viewportBox.width / 2);
    viewport.scrollTop += targetBox.top + targetBox.height / 2 - (viewportBox.top + viewportBox.height / 2);
    // 「強調した位置へ移動」（ボタン・H）を明示したときだけ、画面の外にある強調をページごと最小の移動で見せる（#559）。
    // `page` のビューアは 1 画面より高くなり、強調がページの下の方だと画面の外に出るため。
    // chunk の選択などで自動で強調が変わるときはページを動かさない（右ペインの選択が画面から逃げないように）。
    if (options.revealInPage) target.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, []);

  const hasLayout = layout != null;
  useLayoutEffect(() => {
    if (handledScrollRef.current === scrollRequest || !hasLayout || !loaded) return;
    handledScrollRef.current = scrollRequest;
    scrollToHighlight();
  }, [scrollRequest, hasLayout, loaded, frameWidth, frameHeight, rotation, scrollToHighlight]);

  const rememberAnchor = useCallback((point?: { x: number; y: number }) => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    anchorRef.current = {
      pointX: point?.x ?? viewport.clientWidth / 2,
      pointY: point?.y ?? viewport.clientHeight / 2,
      scrollLeft: viewport.scrollLeft,
      scrollTop: viewport.scrollTop,
      previousWidth: viewport.scrollWidth,
      previousHeight: viewport.scrollHeight,
    };
  }, []);

  const currentZoom = layout?.zoomPercent ?? zoomPercent;
  const zoomTo = useCallback(
    (percent: number, point?: { x: number; y: number }) => {
      rememberAnchor(point);
      setMode("zoom");
      setZoomPercent(clampZoom(percent));
    },
    [rememberAnchor]
  );
  const zoomStep = (direction: "in" | "out") => zoomTo(steppedZoom(currentZoom, direction));
  const fit = (next: PreviewFitMode) => {
    rememberAnchor();
    setMode(next);
  };
  const rotate = (delta: number) => {
    setRotations((current) => ({
      ...current,
      [pageNumber]: normalizeViewRotation((current[pageNumber] ?? 0) + delta),
    }));
    if (rects.length > 0) setScrollRequest((current) => current + 1);
  };
  const goToPage = (target: number) => {
    if (!pageNumbers.includes(target) || target === pageNumber) return;
    setCurrentPage(target);
    const viewport = viewportRef.current;
    if (viewport) {
      viewport.scrollTop = 0;
      viewport.scrollLeft = 0;
    }
    if (highlightPageNumbers.includes(target)) setScrollRequest((current) => current + 1);
  };

  // Ctrl（Mac は ⌘）+ ホイールで拡大縮小。トラックパッドのピンチも同じイベントで届く。
  const zoomRef = useRef({ currentZoom, zoomTo });
  useEffect(() => {
    zoomRef.current = { currentZoom, zoomTo };
  });
  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    const onWheel = (event: WheelEvent) => {
      if (!event.ctrlKey && !event.metaKey) return;
      event.preventDefault();
      const box = viewport.getBoundingClientRect();
      const { currentZoom: zoom, zoomTo: apply } = zoomRef.current;
      apply(zoom * Math.exp(-event.deltaY * 0.002), {
        x: event.clientX - box.left,
        y: event.clientY - box.top,
      });
    };
    viewport.addEventListener("wheel", onWheel, { passive: false });
    return () => viewport.removeEventListener("wheel", onWheel);
  }, []);

  function onKeyDown(event: ReactKeyboardEvent<HTMLDivElement>) {
    if (event.target !== event.currentTarget) return;
    const action = previewKeyAction(event);
    if (!action) return;
    event.preventDefault();
    switch (action) {
      case "zoom-in":
        zoomStep("in");
        break;
      case "zoom-out":
        zoomStep("out");
        break;
      case "fit-page":
        fit("fit-page");
        break;
      case "fit-width":
        fit("fit-width");
        break;
      case "rotate-right":
        rotate(90);
        break;
      case "rotate-left":
        rotate(-90);
        break;
      case "next-page":
        goToPage(pageNumbers[pageIndex + 1] ?? pageNumber);
        break;
      case "previous-page":
        goToPage(pageNumbers[pageIndex - 1] ?? pageNumber);
        break;
      case "first-page":
        goToPage(pageNumbers[0] ?? pageNumber);
        break;
      case "last-page":
        goToPage(pageNumbers[pageNumbers.length - 1] ?? pageNumber);
        break;
      case "focus-highlight":
        scrollToHighlight({ revealInPage: true });
        break;
    }
  }

  // マウスのドラッグでパンする（タッチはブラウザのスクロールのまま）。
  function onPointerDown(event: ReactPointerEvent<HTMLDivElement>) {
    const viewport = viewportRef.current;
    if (!viewport || event.pointerType !== "mouse" || event.button !== 0) return;
    if (viewport.scrollWidth <= viewport.clientWidth && viewport.scrollHeight <= viewport.clientHeight) {
      return;
    }
    dragRef.current = {
      x: event.clientX,
      y: event.clientY,
      left: viewport.scrollLeft,
      top: viewport.scrollTop,
      id: event.pointerId,
    };
    viewport.setPointerCapture?.(event.pointerId);
    setDragging(true);
  }
  function onPointerMove(event: ReactPointerEvent<HTMLDivElement>) {
    const viewport = viewportRef.current;
    const drag = dragRef.current;
    if (!viewport || !drag || drag.id !== event.pointerId) return;
    viewport.scrollLeft = drag.left - (event.clientX - drag.x);
    viewport.scrollTop = drag.top - (event.clientY - drag.y);
  }
  function endDrag(event: ReactPointerEvent<HTMLDivElement>) {
    const drag = dragRef.current;
    if (!drag || drag.id !== event.pointerId) return;
    dragRef.current = null;
    viewportRef.current?.releasePointerCapture?.(event.pointerId);
    setDragging(false);
  }

  const zoomLabel = String(Math.round(currentZoom));
  const canZoomIn = currentZoom < PREVIEW_MAX_ZOOM - 0.5;
  const canZoomOut = currentZoom > PREVIEW_MIN_ZOOM + 0.5;
  const multiPage = pageNumbers.length > 1;
  const overflow =
    layout && viewportSize
      ? layout.frameWidth + stagePadding.x > viewportSize.width + 1 ||
        layout.frameHeight + stagePadding.y > viewportSize.height + 1
      : false;
  const otherHighlightPages = highlightPageNumbers.filter((item) => pageNumbers.includes(item));

  return (
    <div
      data-testid="preview-viewer"
      data-sizing={sizing}
      className={cn(
        "flex min-h-0 flex-col overflow-hidden rounded-md border border-border bg-surface",
        // `page` はビューポートの高さを自分の幅（100cqw）から求めるため、ビューアを container にする。
        sizing === "page" && "@container",
        className
      )}
    >
      <div
        role="toolbar"
        aria-label={labels.toolbar}
        aria-controls={`${hintId}-viewport`}
        // 狭い画面でも 1 行に保ち（ビューアの高さを削らない）、入り切らない分はツールバーの中で横にスクロールする。
        className="flex shrink-0 flex-nowrap items-center gap-1 overflow-x-auto border-b border-border bg-surface px-2 py-1"
      >
        {multiPage ? (
          <div className="flex shrink-0 items-center gap-1">
            <Button
              type="button"
              size="sm"
              variant="ghost"
              iconOnly
              icon={ChevronLeft}
              aria-label={labels.previousPage}
              tooltip={`${labels.previousPage} (PageUp)`}
              disabled={pageIndex <= 0}
              onClick={() => goToPage(pageNumbers[pageIndex - 1] ?? pageNumber)}
            />
            <span
              className="tnum min-w-12 text-center text-xs text-fg-muted"
              aria-label={labels.pageStatusLabel(pageNumber, pageNumbers.length)}
              data-testid="preview-page-status"
            >
              {labels.pageStatus(pageNumber, pageNumbers.length)}
            </span>
            <Button
              type="button"
              size="sm"
              variant="ghost"
              iconOnly
              icon={ChevronRight}
              aria-label={labels.nextPage}
              tooltip={`${labels.nextPage} (PageDown)`}
              disabled={pageIndex >= pageNumbers.length - 1}
              onClick={() => goToPage(pageNumbers[pageIndex + 1] ?? pageNumber)}
            />
            <span aria-hidden className="mx-1 h-5 w-px shrink-0 bg-border" />
          </div>
        ) : null}
        <div className="flex shrink-0 items-center gap-1">
          <Button
            type="button"
            size="sm"
            variant="ghost"
            iconOnly
            icon={ZoomOut}
            aria-label={labels.zoomOut}
            tooltip={`${labels.zoomOut} (-)`}
            disabled={!canZoomOut}
            onClick={() => zoomStep("out")}
          />
          <output
            className="tnum min-w-12 text-center text-xs text-fg-muted"
            aria-label={labels.zoomStatusLabel(zoomLabel)}
            data-testid="preview-zoom-status"
          >
            {labels.zoomStatus(zoomLabel)}
          </output>
          <Button
            type="button"
            size="sm"
            variant="ghost"
            iconOnly
            icon={ZoomIn}
            aria-label={labels.zoomIn}
            tooltip={`${labels.zoomIn} (+)`}
            disabled={!canZoomIn}
            onClick={() => zoomStep("in")}
          />
        </div>
        <span aria-hidden className="mx-1 h-5 w-px shrink-0 bg-border" />
        <div className="flex shrink-0 items-center gap-1">
          <Button
            type="button"
            size="sm"
            variant="ghost"
            iconOnly
            icon={MoveHorizontal}
            aria-label={labels.fitWidth}
            tooltip={`${labels.fitWidth} (W)`}
            pressed={mode === "fit-width"}
            onClick={() => fit("fit-width")}
          />
          <Button
            type="button"
            size="sm"
            variant="ghost"
            iconOnly
            icon={Maximize}
            aria-label={labels.fitPage}
            tooltip={`${labels.fitPage} (0)`}
            pressed={mode === "fit-page"}
            onClick={() => fit("fit-page")}
          />
          <Button
            type="button"
            size="sm"
            variant="ghost"
            iconOnly
            icon={RotateCcw}
            aria-label={labels.rotateLeft}
            tooltip={`${labels.rotateLeft} (Shift+R)`}
            onClick={() => rotate(-90)}
          />
          <Button
            type="button"
            size="sm"
            variant="ghost"
            iconOnly
            icon={RotateCw}
            aria-label={labels.rotateRight}
            tooltip={`${labels.rotateRight} (R)`}
            onClick={() => rotate(90)}
          />
          {highlights.length > 0 ? (
            <Button
              type="button"
              size="sm"
              variant="ghost"
              iconOnly
              icon={LocateFixed}
              aria-label={labels.focusHighlight}
              tooltip={`${labels.focusHighlight} (H)`}
              disabled={rects.length === 0}
              onClick={() => scrollToHighlight({ revealInPage: true })}
            />
          ) : null}
        </div>
      </div>
      {highlights.length > 0 ? (
        <div
          className="flex shrink-0 flex-wrap items-center gap-x-2 gap-y-1 border-b border-border bg-surface-sunken px-3 py-1 text-xs text-fg-muted"
          data-testid="preview-highlight-status"
        >
          {/* 文だけの行も、強調のあるページへのボタン（sm）が並ぶ行と同じ高さにする。
              ページ送りでボタンが出入りしても、ビューアの高さが跳ねないように（#559）。 */}
          <span
            role="status"
            aria-live="polite"
            className="inline-flex min-h-[var(--button-height-sm)] items-center"
          >
            {rects.length > 0
              ? labels.highlightStatus(pageNumber, rects.length)
              : labels.highlightElsewhere}
          </span>
          {otherHighlightPages.length > 1 || (otherHighlightPages.length === 1 && rects.length === 0) ? (
            <span className="flex flex-wrap items-center gap-1" aria-label={labels.highlightPages}>
              {otherHighlightPages.map((item) => (
                <Button
                  key={item}
                  type="button"
                  size="sm"
                  variant="ghost"
                  pressed={item === pageNumber}
                  aria-label={labels.highlightPageLabel(item)}
                  onClick={() => goToPage(item)}
                >
                  {labels.highlightPage(item)}
                </Button>
              ))}
            </span>
          ) : null}
        </div>
      ) : null}
      <div
        ref={viewportRef}
        id={`${hintId}-viewport`}
        role="region"
        tabIndex={0}
        aria-label={labels.region(fileName)}
        aria-describedby={hintId}
        aria-keyshortcuts="+ - 0 W R Shift+R PageUp PageDown Home End H"
        data-testid="preview-viewport"
        data-fit-mode={mode}
        data-rotation={rotation}
        data-page={pageNumber}
        style={
          sizing === "page"
            ? ({ "--preview-page-aspect": String(heightAspect) } as CSSProperties)
            : undefined
        }
        className={cn(
          "relative overflow-auto bg-surface-sunken focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-focus-ring",
          sizing === "page" ? PAGE_SIZED_VIEWPORT_CLASS : "min-h-0 flex-1",
          overflow && (dragging ? "cursor-grabbing select-none" : "cursor-grab")
        )}
        onKeyDown={onKeyDown}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
      >
        {/* 読み込み中は、ほかの領域と同じく領域（ビューポート）の先頭に全幅の行で出す（#2510）。回したページの外に置いて
            文字を回さず（#2474）、高さ 0 の sticky の中に置いて、スクロールしても先頭に残し、ページの位置と高さを動かさない。 */}
        {page && !loaded && !failed ? (
          <div className="pointer-events-none sticky left-0 top-0 z-10 h-0 w-full">
            <div className="border-b border-border bg-surface-sunken p-3">
              <ProcessingIndicator
                active
                label={labels.pageLoading}
                operationKey={`page:${pageNumber}`}
                testId="preview-page-loading"
              />
            </div>
          </div>
        ) : null}
        {/* 余白 p-3 は PAGE_SIZED_VIEWPORT_CLASS の 1.5rem（上下・左右の合計）と合わせる。 */}
        <div ref={stageRef} className="flex min-h-full w-max min-w-full p-3">
          {layout && page ? (
            <div
              className="relative m-auto shrink-0"
              data-testid="preview-page-frame"
              style={{ width: layout.frameWidth, height: layout.frameHeight }}
            >
              <div
                className="absolute left-1/2 top-1/2 overflow-hidden rounded-sm bg-surface shadow-sm"
                data-testid="preview-image-surface"
                style={{
                  width: layout.shellWidth,
                  height: layout.shellHeight,
                  transform: `translate(-50%, -50%) rotate(${rotation}deg)`,
                }}
              >
                {failed ? (
                  <p className="absolute inset-0 flex items-center justify-center p-4 text-center text-sm text-fg-muted">
                    {labels.pageError}
                  </p>
                ) : (
                  <img
                    src={src}
                    alt={`${fileName} p.${pageNumber}`}
                    draggable={false}
                    // 署名つきの URL（Agent の根拠のパネル。#2389）でも、Referer で画面の URL を渡さない。
                    referrerPolicy="no-referrer"
                    // 読み込みが終わるまで描きかけの画像を見せない（点滅する灰色の面から透けてちらつく。#2474）。
                    className={cn(
                      "absolute inset-0 h-full w-full select-none object-contain transition-opacity duration-150 ease-out motion-reduce:transition-none",
                      loaded ? "opacity-100" : "opacity-0"
                    )}
                    onLoad={(event) => {
                      const image = event.currentTarget;
                      setLoadedSources((current) =>
                        current[`${pageNumber}`] ? current : { ...current, [`${pageNumber}`]: true }
                      );
                      if (kind === "image" && image.naturalWidth > 0 && image.naturalHeight > 0) {
                        setNaturalSizes((current) => ({
                          ...current,
                          [pageNumber]: { width: image.naturalWidth, height: image.naturalHeight },
                        }));
                      }
                    }}
                    onError={() => setFailedSources((current) => ({ ...current, [src]: true }))}
                  />
                )}
                {!loaded && !failed ? (
                  <Skeleton className="absolute inset-0 h-full w-full" testId="preview-page-skeleton" />
                ) : null}
                {/* 強調の枠は画像が出てから重ねる（灰色の面の上に枠だけが浮かないように。#2474）。 */}
                {(loaded ? rects : []).map((item) => (
                  <span
                    key={item.key}
                    aria-hidden
                    data-testid="bbox-content-overlay"
                    data-highlight-tone={item.tone}
                    data-bbox-mode={item.rect.coordinateMode}
                    data-bbox-unit={item.rect.unit}
                    title={item.label ?? undefined}
                    className={cn(
                      "pointer-events-none absolute rounded-sm border-2 ring-1 ring-surface/85",
                      item.tone === "primary"
                        ? "border-accent-emphasis bg-accent-emphasis/15"
                        : "border-dashed border-accent-emphasis/70 bg-accent-emphasis/5"
                    )}
                    style={bboxOverlayStyle(item.rect)}
                  />
                ))}
              </div>
            </div>
          ) : (
            <Skeleton className="m-auto h-full min-h-40 w-full" aria-hidden />
          )}
        </div>
      </div>
      <p id={hintId} className="sr-only">
        {labels.keyboardHint}
      </p>
    </div>
  );
}

export function bboxOverlayStyle(rect: BboxOverlayRect): CSSProperties {
  return {
    left: `${rect.leftPercent}%`,
    top: `${rect.topPercent}%`,
    width: `${rect.widthPercent}%`,
    height: `${rect.heightPercent}%`,
  };
}

function preferredPage(
  focusPage: number | null,
  highlightPageNumbers: number[],
  pageNumbers: number[]
): number {
  if (focusPage != null && pageNumbers.includes(focusPage)) return focusPage;
  const highlighted = highlightPageNumbers.find((page) => pageNumbers.includes(page));
  return highlighted ?? pageNumbers[0] ?? 1;
}

function devicePixelRatioValue(): number {
  return typeof window !== "undefined" && window.devicePixelRatio > 0 ? window.devicePixelRatio : 1;
}

/** ステージの内側の余白（px）。フィットの計算でビューポートの実寸から引く。 */
function useStagePadding(ref: RefObject<HTMLDivElement | null>): { x: number; y: number } {
  const [padding, setPadding] = useState({ x: 0, y: 0 });
  useLayoutEffect(() => {
    const stage = ref.current;
    if (!stage || typeof window === "undefined") return;
    const style = window.getComputedStyle(stage);
    const x = parseFloat(style.paddingLeft) + parseFloat(style.paddingRight);
    const y = parseFloat(style.paddingTop) + parseFloat(style.paddingBottom);
    if (Number.isFinite(x) && Number.isFinite(y)) {
      setPadding((current) => (current.x === x && current.y === y ? current : { x, y }));
    }
  }, [ref]);
  return padding;
}
