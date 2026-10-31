import { useId, useMemo, useState } from "react";
import { Tabs, TabPanel } from "../ui/tabs";
import { formatRunDateTime, formatRunDuration } from "./run-history";
import { DataTable } from "./data-table";
import { Disclosure } from "../ui/disclosure";
import { CopyButton } from "../ui/copy-button";
import { MarkdownText } from "../ui/markdown-text";
import { callPresentation, type CallTextBlock } from "../../lib/call-presentation";

import { Info } from "lucide-react";

import { cn } from "../../lib/utils";
import { TEXT_ICON_SIZE } from "../ui/text-icon";

/*
 * 処理の経過の段の中身（#2294）。実行の詳細とチャットの「処理の経過」（`RunTimeline`）で、段を開いて見る詳細の
 * 1 つの形。製品は自分の記録（RAG の回答の記録の工程・NL2SQL のジョブ・Agent のツールの呼び出し）をこの形に
 * 写すだけで、描き方（見出しと値・行・コードの箱・省略の印）は部品が持つ。
 *
 * - `fields`: 見出しと値（エンジン・行数・選んだ条件など）。
 * - `lines`: 補足の行（工程の記録の文など）。
 * - `code`: 長い値（SQL・ツールの引数と結果の JSON）。箱の中で縦横にスクロールし、ページを伸ばさない。
 * - `truncated`: 送る側で長さを切った（内容の一部だけ）。印を文字で出す。
 *
 * 秘密の値（token・API キー・パスワード）は送る側で入れない（backend の `pr_backend_core.chat_progress` の
 * `ChatProgressStepDetails` が長さを切り、秘密らしい値を伏せる）。
 */

export interface RunStepDetailField {
  /** 見出し（翻訳済み）。 */
  label: string;
  value: string;
  /** 識別子・SQL の断片などを等幅で出す。 */
  mono?: boolean;
  /** 共通の span 投影が付ける技術属性。経過では省略し、軌跡に残す。 */
  technical?: boolean;
  copyValue?: string;
}

/** 表示順を変えず、経過と軌跡が同じ内容を使う。 */
export function runStepContent(details: RunStepDetails | null | undefined): RunStepDetails {
  const fields = details?.fields?.filter(field => !field.technical);
  return { fields: fields ? [...fields.filter(field => field.label !== DEFAULT_RUN_STEP_DETAILS_LABELS.result), ...fields.filter(field => field.label === DEFAULT_RUN_STEP_DETAILS_LABELS.result)] : undefined, lines: details?.lines, code: details?.code, truncated: details?.truncated };
}
export function runStepResultSummary(details: RunStepDetails | null | undefined, fallback?: string): string | undefined {
  const result = runStepContent(details).fields?.find(field => field.label === DEFAULT_RUN_STEP_DETAILS_LABELS.result)?.value;
  return [...new Set([fallback, result].filter(Boolean))].join(" · ") || undefined;
}

export function runStepTechnicalFields(details: RunStepDetails | null | undefined): readonly RunStepDetailField[] {
  const fields = [...(details?.fields?.filter(field => field.technical) ?? [])];
  if (details?.call) {
    const labels = DEFAULT_RUN_STEP_DETAILS_LABELS;
    for (const [key, value] of Object.entries(callPresentation(details.call).stats)) {
      if ((typeof value !== "string" && typeof value !== "number") || (key === "model" && fields.some(field => field.label === "gen_ai.request.model"))) continue;
      const label = labels[key as "model" | "inputTokens" | "outputTokens" | "reasoningTokens" | "maxOutput" | "temperature" | "effort" | "requestedDimensions" | "inputType" | "topN"];
      if (label && !fields.some(field => field.label === label && field.value === String(value))) fields.push({label, value: String(value), technical: true});
    }
  }
  return fields;
}

export interface RunStepDetailCode {
  /** 箱の見出し（翻訳済み。例「引数」「結果」「SQL」）。 */
  label?: string;
  /** 中身の種類（読み上げと等幅の表示。色分けはしない）。 */
  language?: "json" | "sql" | "text";
  text: string;
}

export interface CallContent {
  status: "recorded" | "truncated" | "not_recorded" | "withheld";
  text: string;
}

export interface CallDetails {
  capture_enabled?: boolean;
  input: CallContent;
  output: CallContent;
  schema: CallContent;
}

export interface RunStepDetails {
  call?: CallDetails;
  fields?: readonly RunStepDetailField[];
  lines?: readonly string[];
  code?: readonly RunStepDetailCode[];
  /** 送る側で長さを切った（内容の一部だけ）。 */
  truncated?: boolean;
}

/** 中身のある詳細か（無ければ段に開閉のボタンを付けない）。 */
export function hasRunStepDetails(details: RunStepDetails | null | undefined): details is RunStepDetails {
  if (!details) return false;
  return Boolean(
    details.call || details.fields?.some((field) => field.label && field.value) ||
      details.lines?.some((line) => line) ||
      details.code?.some((block) => block.text)
  );
}

/** 2 つの詳細が同じか（処理中の段の一覧の差分の判定）。 */
export function sameRunStepDetails(a: RunStepDetails | undefined, b: RunStepDetails | undefined): boolean {
  if (a === b) return true;
  if (!a || !b) return false;
  return JSON.stringify(a) === JSON.stringify(b);
}

/** 画面で整形するときの JSON の上限（文字数）。backend の上限と同じ。 */
export const RUN_STEP_DETAILS_CODE_MAX_CHARS = 4000;

/**
 * 値を整形した JSON にし、上限を超えたら先頭だけにする（Agent のツールの結果など、画面が持っている値を写すとき）。
 */
export function runStepDetailsJson(
  value: unknown,
  maxChars: number = RUN_STEP_DETAILS_CODE_MAX_CHARS
): { text: string; truncated: boolean } {
  let text: string;
  if (typeof value === "string") {
    text = value;
  } else {
    try {
      text = JSON.stringify(value, null, 2) ?? "";
    } catch {
      text = String(value);
    }
  }
  if (text.length <= maxChars) return { text, truncated: false };
  return { text: `${text.slice(0, maxChars)}…`, truncated: true };
}

export interface RunStepDetailsLabels {
  /** 長さを切ったときの印。 */
  truncated: string;
  disabled: string;
  partial: string;
  requestedDimensions: string;
  inputType: string;
  topN: string;
  vectors: string;
  dimensions: string;
  partialVector: string;
  rank: string;
  score: string;
  document: string;
  sourceIndex: string;
  reasoning: string;
  reasoningSummary: string;
  noReasoning: string;
  raw: string;
  copy: string;
  copied: string;
  copyFailed: string;
  model: string;
  inputTokens: string;
  outputTokens: string;
  reasoningTokens: string;
  maxOutput: string;
  temperature: string;
  effort: string;
  overview: string;
  input: string;
  output: string;
  schema: string;
  notRecorded: string;
  callDetails: string;
  withheld: string;
  snapshotNotice: string;
  timing: string;
  startedAt: string;
  finishedAt: string;
  duration: string;
  result: string;
  copyOriginalTime: string;
  copiedTime: string;
}

export const DEFAULT_RUN_STEP_DETAILS_LABELS: RunStepDetailsLabels = {
  disabled: "本文の記録は設定で無効です。この呼び出しの入力・結果・思考・Schema は保存していません。",
  partial: "完了前に記録された応答です。受信済みの内容を表示しています。", requestedDimensions: "要求した次元数", inputType: "入力の用途", topN: "要求した上位件数",
  vectors: "記録されたベクトル", dimensions: "記録された次元数", partialVector: "一部のみ記録", rank: "順位", score: "関連度", document: "文書", sourceIndex: "元の index",
  reasoning: "思考", reasoningSummary: "思考の要約",
  noReasoning: "表示できる思考テキストがこの記録にはありません。",
  raw: "原始 JSON", copy: "本文をコピー", copied: "本文をコピーしました", copyFailed: "本文をコピーできませんでした。",
  model: "モデル", inputTokens: "入力 token", outputTokens: "出力 token", reasoningTokens: "思考 token",
  maxOutput: "最大出力 token", temperature: "温度", effort: "推論の量",
  truncated: "長い内容は先頭だけを表示しています。",
  timing: "計時",
  startedAt: "開始時刻",
  finishedAt: "終了時刻",
  duration: "所要時間",
  result: "結果",
  copyOriginalTime: "元の ISO 時刻をコピー",
  copiedTime: "元の ISO 時刻をコピーしました",
  overview: "概要",
  input: "入力",
  output: "結果",
  schema: "Schema",
  notRecorded: "この内容は記録されていません。",
  callDetails: "呼び出しの詳細",
  withheld: "現在の権限では、この内容を表示できません。",
  snapshotNotice: "呼び出し時点の記録です。秘密の値・画像の実体は省略しています。",
};

export interface RunStepDetailsViewProps {
  details: RunStepDetails;
  labels?: Partial<RunStepDetailsLabels>;
  className?: string;
  testId?: string;
  showTiming?: boolean;
  showStatistics?: boolean;
  timing?: { startedAt?: string | null; finishedAt?: string | null; durationMs?: number | null };
}

/**
 * 段の詳細を描く（`RunTimeline` の行を開いた中身）。見出しと値 → 行 → コードの箱 → 省略の印の順。
 * コードの箱は高さを止めて中でスクロールし、キーボードでもスクロールできるよう focus を受ける。
 */
function RunStepDetailsBody({ details, labels: labelsInput, className, testId }: RunStepDetailsViewProps) {
  const labels = { ...DEFAULT_RUN_STEP_DETAILS_LABELS, ...labelsInput };
  const fields = (details.fields ?? []).filter((field) => field.label && field.value);
  const lines = (details.lines ?? []).filter(Boolean);
  const code = (details.code ?? []).filter((block) => block.text);
  return (
    <div className={cn("grid min-w-0 gap-2 text-xs leading-5", className)} data-run-step-details="" data-testid={testId}>
      {fields.length ? (
        <dl className="grid min-w-0 gap-x-3 gap-y-1 @md:grid-cols-[max-content_minmax(0,1fr)]">
          {fields.map((field, index) => (
            <div key={`${field.label}-${index}`} className="contents">
              <dt className={cn("min-w-0 break-words text-fg-muted [overflow-wrap:anywhere]", field.copyValue && "flex items-center")}>{field.label}</dt>
              <dd
                className={cn(
                  "min-w-0 whitespace-pre-wrap break-words text-fg [overflow-wrap:anywhere]",
                  field.mono && "font-mono",
                  field.copyValue && "flex flex-wrap items-center gap-2"
                )}
              >
                {field.copyValue ? <time dateTime={field.copyValue} title={field.copyValue}>{field.value}</time> : <span>{field.value}</span>}
                {field.copyValue ? <CopyButton text={field.copyValue} label={`${field.label}: ${labels.copyOriginalTime}`} copiedMessage={labels.copiedTime} failedMessage={labels.copyFailed} size="sm" iconOnly variant="ghost" /> : null}
              </dd>
            </div>
          ))}
        </dl>
      ) : null}
      {lines.length ? (
        <ul className="grid min-w-0 list-disc gap-0.5 pl-4 text-fg">
          {lines.map((line, index) => (
            <li key={`${index}-${line}`} className="break-words [overflow-wrap:anywhere]">
              {line}
            </li>
          ))}
        </ul>
      ) : null}
      {code.map((block, index) => (
        <div key={`${block.label ?? "code"}-${index}`} className="grid min-w-0 gap-1">
          {block.label ? <p className="text-fg-muted">{block.label}</p> : null}
          {/* 長い SQL・JSON は箱の中でスクロールする（ページの横あふれを作らない）。focus を受けてキーで動かせる。 */}
          <pre
            className="max-h-64 w-full min-w-0 max-w-full overflow-auto rounded-md border border-border bg-surface p-2 font-mono text-xs leading-5 text-fg"
            tabIndex={0}
            aria-label={block.label}
            data-language={block.language ?? "text"}
          >
            {block.text}
          </pre>
        </div>
      ))}
      {details.truncated ? (
        <p className="flex min-w-0 items-start gap-1.5 text-fg-muted" data-run-step-details-truncated="">
          <span className="pr-icon-line">
            <Info size={TEXT_ICON_SIZE.xs} aria-hidden="true" />
          </span>
          <span className="min-w-0">{labels.truncated}</span>
        </p>
      ) : null}
    </div>
  );
}

/** システムの表示時刻と元の精密な時刻を同じフィールドで扱う。 */
export function RunStepTiming({startedAt, finishedAt, labels: input}: {startedAt?: string | null; finishedAt?: string | null; labels?: Partial<RunStepDetailsLabels>}) {
  const labels = {...DEFAULT_RUN_STEP_DETAILS_LABELS, ...input};
  return <RunStepDetailsBody details={{fields: [
    {label: labels.startedAt, value: startedAt ? formatRunDateTime(startedAt, {seconds: true, timeZone: true}) : labels.notRecorded, copyValue: startedAt ?? undefined},
    {label: labels.finishedAt, value: finishedAt ? formatRunDateTime(finishedAt, {seconds: true, timeZone: true}) : labels.notRecorded, copyValue: finishedAt ?? undefined},
  ]}} labels={labels} />;
}

/** 呼び出しの内容は選んだタブだけ描画し、大きい JSON を同時に展開しない。 */
export function RunStepDetailsView(props: RunStepDetailsViewProps) {
  const [tab, setTab] = useState("input");
  const id = useId();
  const labels = { ...DEFAULT_RUN_STEP_DETAILS_LABELS, ...props.labels };
  const call = props.details.call;
  const presentation = useMemo(() => call ? callPresentation(call) : null, [call]);
  if (!call) return <RunStepDetailsBody {...props} />;
  const content = tab === "input" ? call.input : tab === "output" || tab === "reasoning" ? call.output : call.schema;
  const blocks = tab === "input" ? presentation?.input : tab === "output" ? presentation?.output : tab === "reasoning" ? presentation?.reasoning : [];
  const semanticOutput = tab === "output" && Boolean(presentation?.embeddings.length || presentation?.ranks.length);
  return <div className="grid min-w-0 gap-2" data-call-details="" data-testid={props.testId}>
    <Tabs idPrefix={id} ariaLabel={labels.callDetails} value={tab} onChange={setTab} items={[
      { id: "overview", label: labels.overview }, { id: "input", label: labels.input },
      { id: "reasoning", label: labels.reasoning }, { id: "output", label: labels.output }, { id: "schema", label: labels.schema },
      ...(props.showTiming === false ? [] : [{ id: "timing", label: labels.timing }]),
    ]} />
    <TabPanel idPrefix={id} id={tab} value={tab}>
      {tab === "overview" ? <RunStepDetailsBody {...props} details={{ ...props.details, fields: [...(props.details.fields ?? []), ...Object.entries(props.showStatistics === false ? {} : presentation?.stats ?? {}).flatMap(([key, value]) => typeof value === "string" || typeof value === "number" ? [{ label: labels[key as "model" | "inputTokens" | "outputTokens" | "reasoningTokens" | "maxOutput" | "temperature" | "effort" | "requestedDimensions" | "inputType" | "topN"], value: String(value) }] : [])] }} testId={undefined} /> :
        tab === "timing" ? <RunStepDetailsBody details={{ fields: [
          { label: labels.startedAt, value: props.timing?.startedAt ? formatRunDateTime(props.timing.startedAt, { seconds: true, timeZone: true }) : labels.notRecorded },
          { label: labels.finishedAt, value: props.timing?.finishedAt ? formatRunDateTime(props.timing.finishedAt, { seconds: true, timeZone: true }) : labels.notRecorded },
          { label: labels.duration, value: formatRunDuration(props.timing?.durationMs, labels.notRecorded) },
        ] }} labels={labels} /> :
        content.status === "withheld" ? <p className="text-xs text-fg-muted">{labels.withheld}</p> :
        call.capture_enabled === false ? <p className="text-xs text-fg-muted">{labels.disabled}</p> :
        content.status === "not_recorded" ? <p className="text-xs text-fg-muted">{labels.notRecorded}</p> :
        <div className="grid min-w-0 gap-3">
          {(tab === "output" || tab === "reasoning") && presentation?.partial ? <p className="text-xs text-fg-muted">{labels.partial}</p> : null}
          {semanticOutput && presentation ? <div className="grid min-w-0 gap-3" data-call-semantic="">
            {presentation.embeddings.length ? <>
              <p className="text-xs text-fg-muted">{labels.vectors}: {presentation.embeddings.length}</p>
              <CallTextBlocks blocks={presentation.embeddings.map(vector => ({ role: `#${vector.index} · ${labels.dimensions}: ${vector.dimensions}${vector.partial ? ` · ${labels.partialVector}` : ""}`, text: vector.values, collapsed: true }))} labels={labels} />
            </> : null}
            {presentation.ranks.length ? <DataTable rows={presentation.ranks} getRowKey={row => row.rank} dense visibleRows={5} scrollAriaLabel={labels.output} columns={[
              { key: "rank", header: labels.rank }, { key: "index", header: labels.sourceIndex }, { key: "score", header: labels.score },
              { key: "text", header: labels.document, render: row => <Disclosure size="sm" variant="plain" summary={<span className="line-clamp-2 break-words [overflow-wrap:anywhere]">{row.text || labels.notRecorded}</span>}><div className="grid min-w-0 gap-2"><CopyButton text={row.text} label={labels.copy} copiedMessage={labels.copied} failedMessage={labels.copyFailed} size="sm" variant="ghost" /><CallBody text={row.text} /></div></Disclosure> },
            ]} /> : null}
          </div> : blocks?.length ? <CallTextBlocks key={tab} blocks={blocks} labels={labels} /> : tab === "reasoning" ? <p className="text-xs text-fg-muted">{labels.noReasoning}</p> :
            <RunStepDetailsBody details={{ code: [{ text: content.text === "" ? '""' : content.text, language: "json", label: labels[tab as "input" | "output" | "schema"] }] }} labels={labels} />}
          {content.status === "truncated" ? <p className="text-xs text-fg-muted">{labels.truncated}</p> : null}
          {semanticOutput || blocks?.length || tab === "reasoning" ? <Disclosure summary={labels.raw} size="sm" variant="plain"><RunStepDetailsBody details={{ code: [{ text: content.text, language: "json", label: labels.raw }] }} /></Disclosure> : null}
        </div>}
    </TabPanel>
    <p className="text-xs text-fg-muted">{labels.snapshotNotice}</p>
  </div>;
}


function CallTextBlocks({ blocks, labels }: { blocks: readonly CallTextBlock[]; labels: RunStepDetailsLabels }) {
  return <div className="grid max-h-[36rem] min-w-0 gap-3 overflow-y-auto" tabIndex={0} aria-label={labels.callDetails} data-call-readable="">
    {blocks.map((block, index) => <Disclosure key={index} summary={block.role === "reasoning" ? labels.reasoning : block.role === "summary" ? labels.reasoningSummary : block.role} defaultOpen={!block.collapsed && block.role !== "system" && block.role !== "developer"} size="sm">
      <div className="grid min-w-0 gap-2">
        <CopyButton text={block.text} label={labels.copy} copiedMessage={labels.copied} failedMessage={labels.copyFailed} size="sm" variant="ghost" />
        <div data-call-body="" className="min-w-0"><CallBody text={block.text} /></div>
      </div>
    </Disclosure>)}
  </div>;
}


function CallBody({ text }: { text: string }) {
  let json: string | null = null;
  if (/^\s*[\[{]/.test(text)) {
    try { json = JSON.stringify(JSON.parse(text), null, 2); } catch { /* 不完全な記録は本文のまま表示する。 */ }
  }
  return json ? <pre className="min-w-0 whitespace-pre-wrap break-words font-mono text-xs leading-5 text-fg [overflow-wrap:anywhere]">{json}</pre> :
    <MarkdownText text={text} className="text-xs leading-5 [&_h3]:text-xs [&_h4]:text-xs [&_h5]:text-xs [&_h6]:text-xs" />;
}
