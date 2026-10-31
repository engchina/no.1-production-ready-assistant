const WEB_VITALS_START_TIME_ERROR = "Cannot read properties of undefined (reading 'startTime')";
const DEVTOOLS_WEB_VITALS_FRAME_PATTERN = /\breportAllChanges\b[\s\S]*(?:<anonymous>|VM\d+|chrome-devtools)/iu;

type BrowserErrorCandidate = {
  message?: unknown;
  filename?: unknown;
  error?: unknown;
  reason?: unknown;
};

export function isDevToolsWebVitalsStartTimeError(candidate: unknown): boolean {
  const message = extractMessage(candidate);
  if (!message.includes(WEB_VITALS_START_TIME_ERROR)) return false;

  const diagnosticText = [
    message,
    extractStack(candidate),
    extractFilename(candidate),
  ]
    .filter(Boolean)
    .join("\n");

  return DEVTOOLS_WEB_VITALS_FRAME_PATTERN.test(diagnosticText);
}

export function installBrowserErrorGuards(target: Window = window): () => void {
  const handleError = (event: ErrorEvent) => {
    if (isDevToolsWebVitalsStartTimeError(event)) {
      // Chrome DevTools が注入する web-vitals の既知例外だけを console 表示から外す。
      event.preventDefault();
      event.stopImmediatePropagation();
    }
  };
  const handleUnhandledRejection = (event: PromiseRejectionEvent) => {
    if (isDevToolsWebVitalsStartTimeError(event.reason)) {
      event.preventDefault();
      event.stopImmediatePropagation();
    }
  };

  target.addEventListener("error", handleError, { capture: true });
  target.addEventListener("unhandledrejection", handleUnhandledRejection, { capture: true });

  return () => {
    target.removeEventListener("error", handleError, { capture: true });
    target.removeEventListener("unhandledrejection", handleUnhandledRejection, { capture: true });
  };
}

function extractMessage(candidate: unknown): string {
  if (typeof candidate === "string") return candidate;
  if (candidate instanceof Error) return candidate.message;
  if (!isRecord(candidate)) return "";

  const errorCandidate = candidate.error ?? candidate.reason;
  const directMessage = typeof candidate.message === "string" ? candidate.message : "";
  const nestedMessage = extractMessage(errorCandidate);
  return [directMessage, nestedMessage].filter(Boolean).join("\n");
}

function extractStack(candidate: unknown): string {
  if (candidate instanceof Error) return candidate.stack ?? "";
  if (!isRecord(candidate)) return "";

  const errorCandidate = candidate.error ?? candidate.reason;
  const ownStack = typeof candidate.stack === "string" ? candidate.stack : "";
  const nestedStack = extractStack(errorCandidate);
  return [ownStack, nestedStack].filter(Boolean).join("\n");
}

function extractFilename(candidate: unknown): string {
  if (!isRecord(candidate)) return "";
  const filename = typeof candidate.filename === "string" ? candidate.filename : "";
  const nestedFilename = extractFilename(candidate.error ?? candidate.reason);
  return [filename, nestedFilename].filter(Boolean).join("\n");
}

function isRecord(value: unknown): value is BrowserErrorCandidate & Record<string, unknown> {
  return typeof value === "object" && value !== null;
}
