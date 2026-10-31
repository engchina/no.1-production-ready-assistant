import { RUN_TIME_TRACK, RUN_TIME_FILL, RUN_TIME_COLOR, runTimelineNameClass } from "./run-timeline-style";
import { ArrowLeft, ArrowDown, ArrowUp, Bot, Layers, Wrench } from "lucide-react";
import { useEffect, useId, useMemo, useRef, useState } from "react";

import { operationTimestampMs } from "../../lib/operation-timing";
import { traceCategory, traceMeasuredTiming, traceOrderedSteps, traceSearchText, type TraceCategory, type TraceOrder } from "../../lib/trace-explorer";
import { cn } from "../../lib/utils";
import { Button } from "../ui/button";
import { CopyButton } from "../ui/copy-button";
import { SearchField } from "../ui/search-field";
import { Tabs, TabPanel } from "../ui/tabs";
import { FilterSelect } from "./list-filters";
import { ListToolbar } from "./list-toolbar";
import { LoadMoreFooter } from "./load-more-footer";
import { formatRunDuration } from "./run-history";
import { RUN_TIMELINE_CHEVRON_SIZE, resolveRunDetailLabels, RunStepStatusIcon, stepDurationMs, RunTimelineViewport, runTimelineStepIndentStyle, type RunDetailLabelsInput, type RunTimelineStep } from "./run-detail";
import { RunStepDetailsView, RunStepTiming, runStepContent, runStepTechnicalFields, runStepResultSummary } from "./run-step-details";


const CATEGORY_ICON = { model: Bot, tool: Wrench, stage: Layers };

/** 共有 UI の既定の日本語。製品は翻訳済みの文言で上書きできる。 */
export const DEFAULT_TRACE_EXPLORER_LABELS = {
  open: "軌跡を調べる", title: "軌跡の調査", back: "経過に戻る", search: "呼び出しを検索", placeholder: "名前・ID・記録した内容", clear: "検索語をクリア",
  category: "種類", status: "状態", all: "すべて", failed: "失敗", active: "処理中", model: "モデル", tool: "ツール", stage: "工程",
  hierarchy: "階層", time: "開始時刻順", order: "呼び出しの並び", overview: "時間の概観", measured: "開始時刻を基準にした実測の位置です。",
  unknown: "時刻が記録されていない呼び出しは、時間の概観に表示していません。", inflight: "終了時刻のない呼び出しは開始位置だけを表示します。",
  empty: "条件に一致する呼び出しがありません。", reset: "条件をクリア", choose: "呼び出しを選ぶと詳細が表示されます。",
  list: "呼び出しの一覧", backToList: "一覧に戻る", previous: "前の呼び出し", next: "次の呼び出し", noDetails: "呼び出しの内容は記録されていません。",
  copy: "ID をコピー", copied: "ID をコピーしました", copyFailed: "ID をコピーできませんでした。", more: "さらに表示", retry: "再試行",
  loaded: (shown: number, total: number) => `${shown} / ${total} 件を表示`,
};
export type TraceExplorerLabels = typeof DEFAULT_TRACE_EXPLORER_LABELS;



export function TraceExplorer({ steps, onClose, labels: detailLabels, explorerLabels, size = "md" }: {
  size?: "sm" | "md";
  steps: readonly RunTimelineStep[];
  onClose: () => void;
  labels?: RunDetailLabelsInput;
  explorerLabels?: Partial<TraceExplorerLabels>;
}) {
  const labels = { ...DEFAULT_TRACE_EXPLORER_LABELS, ...explorerLabels };
  const runLabels = resolveRunDetailLabels(detailLabels);
  const id = useId();
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState<TraceCategory | "all">("all");
  const [status, setStatus] = useState("all");
  const [order, setOrder] = useState<TraceOrder>("hierarchy");
  const [selectedId, setSelectedId] = useState<string | null>(() => steps.find(step => step.status === "failed")?.id ?? steps.find(step => step.details?.call && traceCategory(step) === "model")?.id ?? steps.find(step => step.details?.call)?.id ?? steps[0]?.id ?? null);
  const [mobileDetail, setMobileDetail] = useState(false);
  const [limit, setLimit] = useState(50);
  const listRef = useRef<HTMLOListElement>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  useEffect(() => { headingRef.current?.focus({ preventScroll: true }); }, []);
  const titleRef = useRef<HTMLHeadingElement>(null);
  const searchIndex = useMemo(() => new Map(steps.map(step => [step.id, traceSearchText(step)])), [steps]);
  const timing = useMemo(() => traceMeasuredTiming(steps), [steps]);
  const filtered = traceOrderedSteps(steps, order).filter(step =>
    (category === "all" || traceCategory(step) === category) &&
    (status === "all" || (status === "active" ? step.status === "running" || step.status === "waiting" : step.status === "failed")) &&
    (!query || searchIndex.get(step.id)?.includes(query.toLocaleLowerCase())));
  const selected = filtered.find(step => step.id === selectedId) ?? filtered[0];
  const selectedIndex = selected ? filtered.indexOf(selected) : -1;
  useEffect(() => {
    const button = listRef.current?.querySelector<HTMLButtonElement>('[aria-pressed="true"]');
    const viewport = listRef.current?.closest<HTMLElement>("[data-trace-list-scroll]");
    if (!button || !viewport || viewport.clientHeight === 0) return;
    const row = button.getBoundingClientRect();
    const box = viewport.getBoundingClientRect();
    if (row.top < box.top) viewport.scrollTop -= box.top - row.top;
    else if (row.bottom > box.bottom) viewport.scrollTop += row.bottom - box.bottom;
  }, [selected?.id, order, limit]);
  const hasFilters = Boolean(query || category !== "all" || status !== "all");
  function reset() { setQuery(""); setCategory("all"); setStatus("all"); setLimit(50); }
  function select(step: RunTimelineStep) {
    setSelectedId(step.id); setMobileDetail(true);
    requestAnimationFrame(() => titleRef.current?.focus({ preventScroll: true }));
  }
  function move(delta: number) {
    const step = filtered[selectedIndex + delta];
    if (!step) return;
    setLimit(value => Math.max(value, selectedIndex + delta + 1));
    setSelectedId(step.id);
  }
  const selectedParents: RunTimelineStep[] = [];
  const visited = new Set<string>(selected ? [selected.id] : []);
  let parent = selected?.parentId;
  while (parent && !visited.has(parent)) {
    visited.add(parent);
    const step = steps.find(item => item.id === parent);
    if (!step) break;
    selectedParents.unshift(step); parent = step.parentId;
  }
  // 選択した行を含む単位まで表示し、詳細から一覧へフォーカスを戻せるようにする。
  const visibleLimit = Math.max(limit, Math.ceil((selectedIndex + 1) / 50) * 50);
  const list = filtered.slice(0, visibleLimit);
  return <section className="@container grid min-w-0 gap-3" aria-label={labels.title} data-trace-explorer="">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <h3 ref={headingRef} tabIndex={-1} className="text-sm font-semibold text-fg">{labels.title}</h3>
      <Button variant="ghost" size="sm" icon={ArrowLeft} onClick={onClose}>{labels.back}</Button>
    </div>
    <div className={cn("grid min-w-0 gap-3", mobileDetail && "hidden @4xl:grid")}>
    <div className="grid min-w-0 gap-2 rounded-md border border-border bg-surface p-3" data-trace-overview="">
      <div className="flex items-center justify-between gap-2 text-xs text-fg-muted"><span>{labels.overview}</span><span className="tnum">0 — {formatRunDuration(timing.duration, runLabels.notRecorded)}</span></div>
      {(["stage", "model", "tool"] as const).map(type => {
        const Icon = CATEGORY_ICON[type];
        return <div key={type} className="grid grid-cols-[5rem_minmax(0,1fr)] items-center gap-2">
          <span className={cn("flex items-center gap-1.5 text-fg-muted", runTimelineNameClass(size))}><Icon size={14} aria-hidden="true" />{labels[type]}</span>
          <div className={RUN_TIME_TRACK} data-trace-time-bar="" aria-hidden="true">
            {steps.map((step, index) => {
              const bar = timing.bars[index];
              if (traceCategory(step) !== type || !bar) return null;
              return <span key={step.id} data-trace-overview-span={step.id} data-trace-overview-selected={step.id === selected?.id ? "true" : undefined} className={cn(RUN_TIME_FILL, RUN_TIME_COLOR[step.status], step.id === selected?.id && "z-10 outline-2 outline-offset-2 outline-focus-ring")} style={{ left: `${bar.offset}%`, width: bar.width == null ? "0%" : `${bar.width}%` }} />;
            })}
          </div>
        </div>;
      })}
      <p className="text-xs text-fg-muted">{labels.measured}{steps.some(step => operationTimestampMs(step.startedAt) == null) ? ` ${labels.unknown}` : ""}{steps.some(step => step.startedAt && !step.finishedAt) ? ` ${labels.inflight}` : ""}</p>
    </div>
    <ListToolbar search={<SearchField id={`${id}-search`} value={query} onSearch={value => { setQuery(value); setLimit(50); }} onDraftChange={() => setMobileDetail(false)} label={labels.search} placeholder={labels.placeholder} clearLabel={labels.clear} />}
      filters={<><FilterSelect id={`${id}-category`} label={labels.category} value={category} onValueChange={value => { setCategory(value); setLimit(50); setMobileDetail(false); }} options={(["all", "model", "tool", "stage"] as const).map(value => ({value, label: labels[value]}))} />
        <FilterSelect id={`${id}-status`} label={labels.status} value={status} onValueChange={value => { setStatus(value); setLimit(50); setMobileDetail(false); }} options={["all", "failed", "active"].map(value => ({value, label: labels[value as "all" | "failed" | "active"]}))} /></>}
      count={{total: steps.length, matched: hasFilters ? filtered.length : undefined}}
      searchActive={Boolean(query)} activeFilterCount={Number(category !== "all") + Number(status !== "all")} onClearFilters={reset} />
    <Tabs idPrefix={`${id}-order`} value={order} onChange={value => { setOrder(value as TraceOrder); setLimit(50); }} ariaLabel={labels.order} items={[{id: "hierarchy", label: labels.hierarchy}, {id: "time", label: labels.time}]} />
    </div>
    <TabPanel idPrefix={`${id}-order`} id={order} value={order}>
      {!filtered.length ? <div className="grid gap-2 rounded-md border border-border p-4 text-sm"><p role="status">{labels.empty}</p>{hasFilters ? <Button variant="secondary" onClick={reset}>{labels.reset}</Button> : null}</div> :
        <div className="grid min-w-0 items-start gap-3 @4xl:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
          <div className={cn("min-w-0", mobileDetail ? "hidden @4xl:block" : "block")}>
            <div data-trace-list-scroll="" className="max-h-[36rem] overflow-y-auto rounded-md border border-border bg-surface">
              <RunTimelineViewport depth={order === "hierarchy" ? Math.max(0, ...list.map(step => step.level ?? 0)) : 0} label={labels.list}>
                <ol ref={listRef} aria-label={labels.list} className="grid min-w-0 divide-y divide-border">
                  {list.map(step => {
                    const index = steps.indexOf(step); const bar = timing.bars[index];
                    return <li key={step.id} data-trace-record={step.id} data-trace-category={traceCategory(step)} data-level={step.level ?? 0} data-parent-id={step.parentId} data-status={step.status} style={order === "hierarchy" ? runTimelineStepIndentStyle(step.level) : undefined}>
                      <button type="button" data-surface-tint={selected?.id === step.id ? "accent" : undefined} aria-pressed={selected?.id === step.id} onClick={() => select(step)}
                        className={cn("relative grid min-h-[var(--control-height-touch)] w-full min-w-0 cursor-pointer gap-1 border-l-2 p-2 text-left hover:bg-surface-hover", selected?.id === step.id ? "border-l-accent-fg bg-accent-muted" : "border-l-transparent")}
                        onKeyDown={event => {
                          const offset = event.key === "ArrowDown" ? 1 : event.key === "ArrowUp" ? -1 : 0;
                          if (!offset) return;
                          const buttons = listRef.current?.querySelectorAll<HTMLButtonElement>("button");
                          const next = buttons?.[list.indexOf(step) + offset];
                          if (next) { event.preventDefault(); next.focus(); }
                        }}>
                        <span className={cn("flex min-w-0 items-start gap-2 leading-5", runTimelineNameClass(size))}><span className="pr-icon-line"><RunStepStatusIcon status={step.status} size={RUN_TIMELINE_CHEVRON_SIZE[size]} spin={false} /></span><span data-trace-record-label="" className="min-w-0 flex-1 break-words text-fg [overflow-wrap:anywhere]">{step.label}</span></span>
                        <span className="flex items-center justify-between gap-2 text-xs text-fg-muted"><span data-trace-step-status-duration="">{runLabels.stepStatus[step.status]}{stepDurationMs(step) !== null ? ` · ${formatRunDuration(stepDurationMs(step), runLabels.notRecorded)}` : ""}</span></span>
                        {runStepResultSummary(step.details, step.detail) ? <span data-trace-result-summary="" className="text-xs leading-5 text-fg-muted">{runStepResultSummary(step.details, step.detail)}</span> : null}
                        {bar ? <span className={cn(RUN_TIME_TRACK, "overflow-hidden")} data-trace-time-bar="" aria-hidden="true"><span className={cn(RUN_TIME_FILL, RUN_TIME_COLOR[step.status])} style={{left: `${bar.offset}%`, width: bar.width == null ? "0%" : `${bar.width}%`}} /></span> : null}
                      </button>
                    </li>;
                  })}
                </ol>
              </RunTimelineViewport>
            </div>
            {list.length < filtered.length ? <LoadMoreFooter summary={labels.loaded(list.length, filtered.length)} hasMore onLoadMore={() => setLimit(visibleLimit + 50)} loadMoreLabel={labels.more} retryLabel={labels.retry} /> : null}
          </div>
          <section className={cn("min-w-0 rounded-md border border-border bg-surface p-3 @4xl:sticky @4xl:top-3", mobileDetail ? "block" : "hidden @4xl:block")} aria-labelledby={`${id}-selected`} data-trace-inspector="">
            <div className="mb-3 flex flex-wrap justify-between gap-2">
              <Button variant="ghost" size="sm" className="@4xl:hidden" icon={ArrowLeft} onClick={() => { setMobileDetail(false); requestAnimationFrame(() => listRef.current?.querySelector<HTMLButtonElement>('[aria-pressed="true"]')?.focus()); }}>{labels.backToList}</Button>
              <div className="flex gap-1"><Button variant="ghost" size="sm" icon={ArrowUp} iconOnly aria-label={labels.previous} disabled={selectedIndex <= 0} onClick={() => move(-1)} /><Button variant="ghost" size="sm" icon={ArrowDown} iconOnly aria-label={labels.next} disabled={selectedIndex < 0 || selectedIndex >= filtered.length - 1} onClick={() => move(1)} /></div>
            </div>
            <h4 id={`${id}-selected`} ref={titleRef} tabIndex={-1} className={cn("break-words font-normal leading-5 text-fg [overflow-wrap:anywhere]", runTimelineNameClass(size))}>{selected?.label ?? labels.choose}</h4>
            {selected ? <>
              {selectedParents.length ? <p className="mt-1 break-words text-xs text-fg-muted [overflow-wrap:anywhere]">{selectedParents.map(step => step.label).join(" / ")}</p> : null}
              <p className="my-2 flex min-w-0 items-center gap-2 text-xs leading-5 text-fg-muted"><RunStepStatusIcon status={selected.status} spin={false} size={RUN_TIMELINE_CHEVRON_SIZE[size]} />{runLabels.stepStatus[selected.status]} · {formatRunDuration(stepDurationMs(selected), runLabels.notRecorded)}</p>
              <div className="mb-3 flex flex-wrap items-center gap-2 text-xs"><span>Span ID:</span><code className="min-w-0 break-all font-mono text-xs text-fg-muted">{selected.id}</code><CopyButton text={selected.id} label={labels.copy} copiedMessage={labels.copied} failedMessage={labels.copyFailed} size="sm" iconOnly variant="ghost" /></div>
              <div className="grid min-w-0 gap-2" data-trace-inspector-content="">
                <div data-trace-processing-content="" className="grid min-w-0 gap-2">
                  {selected.detail ? <p className="text-xs leading-5 text-fg-muted">{selected.detail}</p> : null}
                  <RunStepDetailsView showStatistics={false} showTiming={false} details={{...runStepContent(selected.details), call: selected.details?.call}} timing={{startedAt: selected.startedAt, finishedAt: selected.finishedAt, durationMs: stepDurationMs(selected)}} />
                  {selected.children}
                </div>
                <div data-trace-timing=""><RunStepTiming startedAt={selected.startedAt} finishedAt={selected.finishedAt} /></div>
                <div data-trace-technical=""><RunStepDetailsView details={{fields: runStepTechnicalFields(selected.details)}} /></div>
              </div>
            </> : null}
          </section>
        </div>}
    </TabPanel>
  </section>;
}
