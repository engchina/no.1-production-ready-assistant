import { logBrowserDiagnostic, type BrowserDiagnostic } from "./diagnostic-log";

/**
 * 最初の描画に要らない CSS を、最初の描画の後に読む（#3305）。
 *
 * 主の CSS は描画を止める資源のため、最初の表示に要らない規則（Noto Sans JP に無い字を補う Noto Sans SC・TC・HK の
 * `@font-face` など）は製品の `main.tsx` で `import()` に分け、Vite が別の CSS の chunk にする。
 * 読む時機は 3 製品で同じ: `container`（React の root）に最初の子が入った（最初の commit）後の、次の frame の後。
 * 背景のタブなど `requestAnimationFrame` が進まないときも、`fallbackMs` の後には読む。
 * 読み込みの失敗は描画を壊さない（字は前の書体のまま出る）。診断の WARNING だけを出す。
 */
export interface DeferredStylesOptions {
  serviceName: BrowserDiagnostic["serviceName"];
  /** 最初の commit の後、frame が進まなくても読むまでの ms（既定 2000）。 */
  fallbackMs?: number;
}

const DEFAULT_FALLBACK_MS = 2000;

/** `load`（`() => import("./fonts-cjk.css")` など）を最初の描画の後に 1 回だけ呼ぶ。戻り値で取り消せる（読む前だけ）。 */
export function loadStylesAfterFirstRender(
  container: Element,
  load: () => Promise<unknown>,
  { serviceName, fallbackMs = DEFAULT_FALLBACK_MS }: DeferredStylesOptions
): () => void {
  let state: "waiting" | "scheduled" | "done" = "waiting";
  let observer: MutationObserver | undefined;
  let frame: number | undefined;
  let afterFrame: ReturnType<typeof setTimeout> | undefined;
  let fallback: ReturnType<typeof setTimeout> | undefined;

  const cleanup = () => {
    observer?.disconnect();
    if (frame !== undefined) cancelAnimationFrame(frame);
    if (afterFrame !== undefined) clearTimeout(afterFrame);
    if (fallback !== undefined) clearTimeout(fallback);
  };
  const fail = (error: unknown) => logBrowserDiagnostic("WARNING", { event: "deferred_styles_failed", serviceName, error });
  const run = () => {
    if (state === "done") return;
    state = "done";
    cleanup();
    try {
      load().catch(fail);
    } catch (error) {
      fail(error);
    }
  };
  const schedule = () => {
    if (state !== "waiting") return;
    state = "scheduled";
    observer?.disconnect();
    // rAF は次の frame の描画の直前に動き、その中の setTimeout はその frame の描画の後に動く。
    frame = requestAnimationFrame(() => {
      afterFrame = setTimeout(run, 0);
    });
    fallback = setTimeout(run, fallbackMs);
  };

  if (container.firstChild) {
    schedule();
  } else {
    observer = new MutationObserver(() => {
      if (container.firstChild) schedule();
    });
    observer.observe(container, { childList: true });
  }
  return () => {
    if (state === "done") return;
    state = "done";
    cleanup();
  };
}
