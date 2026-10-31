import { BookOpen, ExternalLink, FileText, History, Layers, LocateFixed, Lock, type LucideIcon } from "lucide-react";
import { useCallback, useEffect, useRef, useState, type ReactNode, type RefObject } from "react";

import { resolveContentLang } from "../../lib/content-lang";
import { excerptPlainText } from "../../lib/excerpt-text";
import { INFORMATION_TABLE_FOCUS_CLASS } from "../../lib/list-density";
import { cn } from "../../lib/utils";
import { ExcerptContent } from "../data/excerpt-content";
import { StatusBadge } from "../data/status-badge";
import { Banner } from "../ui/banner";
import { Button } from "../ui/button";
import { Disclosure } from "../ui/disclosure";
import { SideSheet } from "../ui/side-sheet";
import { TEXT_ICON_SIZE } from "../ui/text-icon";

/**
 * 回答の根拠（AI に渡した箇所の一覧。RAG のチャット・RAG 検索・Agent のチャットで共通。#2328）。
 *
 * 製品は自分の根拠の形（RAG の `RetrievedChunk`・Agent の成果物 `answer_evidence`）を
 * {@link EvidenceListItem} に写して渡す。文言は 3 製品で同じ書き方にするため {@link evidenceLabels} の既定を使う。
 */

/** 回答での使われ方。confirmed は質問の条件への該当確認済みの記載、reference は該当が確認できていない参考の記載。 */
export type EvidenceAnswerUse = "confirmed" | "reference";

/** 根拠の場所・種類のチップ（頁・節・内容の種類など。翻訳済み）。 */
export interface EvidenceChip {
  id: string;
  /** 読み上げの名前（例:「ページ」）。画面には出さない。 */
  label: string;
  /** 表示する値（例:「p.3」）。幅で切れたときも title で全文を確かめられる。 */
  value: string;
}

/** チップの後ろに並べる製品の補足（RAG の処理レシピ・文書の分類など）。 */
export interface EvidenceTag {
  id: string;
  value: string;
  icon?: LucideIcon;
  /** info は分類の色の面（色だけで意味を持たせない）。既定は neutral。 */
  tone?: "neutral" | "info";
}

/** 根拠 1 件のデータの契約（製品に依らない）。 */
export interface EvidenceListItem {
  /** 一覧の中で一意の key（chunk の id など）。 */
  key: string;
  /** 文書名（無ければ製品が文書の id などを入れる）。 */
  fileName: string;
  /** 本文（抜粋）。カードでは 3 行まで出す。 */
  text: string;
  /**
   * 本文の言語（BCP 47。#3335）。取込で分かっていれば渡す。渡さなければ本文から判定して抜粋に `lang` を付け、
   * `null` なら付けない（`resolveContentLang`）。
   */
  lang?: string | null;
  /**
   * 回答で引用した箇所か。回答を作らない検索の結果など、引用したかが分からないときは null
   * （見出しは件数だけにし、バッジを出さない）。
   */
  usedInAnswer: boolean | null;
  /** 回答での使われ方。引用していない・分からなければ null。 */
  answerUse: EvidenceAnswerUse | null;
  /**
   * 箇所そのものは引用していないが、回答がその箇所を含むまとまり（親）を参照したか（#2710）。引用した箇所が
   * 決まらなかった参照の候補で、「回答で引用」とは分けて数え、「まとまりで参照」のバッジを出す。
   */
  usedAsContext?: boolean;
  /** 新しい版に置き換えた文書（旧版）の根拠か。 */
  superseded?: boolean;
  /** 検索の経路のバッジ（翻訳済み。例:「Vector #1」「Rerank #2」）。 */
  routeBadges?: readonly string[];
  /** 取得のスコア。分からなければ null（「—」）。 */
  score: number | null;
  /** Rerank のスコア（0〜1）。Rerank していなければ null。 */
  rerankScore: number | null;
  chips?: readonly EvidenceChip[];
  tags?: readonly EvidenceTag[];
  /** 原典の URL（https のときだけ渡す）。 */
  sourceUrl?: string | null;
  /**
   * 「根拠を表示」で開く原文が無い根拠（チャットの添付など。#2517）。true なら開くボタンを出さない
   * （本文はカードに出ている）。
   */
  sourceUnavailable?: boolean;
}

/** 文言（すべて翻訳済み）。既定は {@link evidenceLabels}。 */
export interface EvidenceLabels {
  /** 見出しの件数（例:「AI に渡した 12 件」）。 */
  count: (count: number) => string;
  /** 例:「回答で引用 3 件」。 */
  used: (count: number) => string;
  /** 例:「うち該当確認済み 2 件」。 */
  confirmed: (count: number) => string;
  /** 例:「参考 1 件」。 */
  reference: (count: number) => string;
  /** 例:「まとまりで参照 2 件」（回答がまとまりだけを参照した箇所。#2710）。 */
  context: (count: number) => string;
  /**
   * 例:「権限の無い根拠 2 件」。保存した根拠のうち、見ている利用者が今の権限で見られない根拠の数（抜粋・資料名は
   * 出さない。#3252）。
   */
  withheld: (count: number) => string;
  /** 見られない根拠の数の行の補足（なぜ中身を出さないか）。 */
  withheldNote: string;
  separator: string;
  /** 回答で引用した箇所のバッジ。 */
  usedBadge: string;
  /** 参考の記載として引用した箇所のバッジ。 */
  referenceBadge: string;
  /** 回答がまとまり（親）だけを参照した箇所のバッジ（#2710）。 */
  contextBadge: string;
  superseded: string;
  /** 経路のバッジの「Evidence」（AI に渡した箇所の印）。 */
  evidenceBadge: string;
  scoreRetrieval: string;
  scoreRerank: string;
  scoreRerankMissing: string;
  scoreMeter: (label: string, value: string) => string;
  openSource: string;
  /** 段落の下の根拠の行（例:「根拠：文書 A.pdf p.3」）。`location` は空のことがある。 */
  citation: (fileName: string, location: string) => string;
  /** 押せる根拠の行の読み上げ（例:「根拠：文書 A.pdf p.3 を表示」）。 */
  citationOpen: (citation: string) => string;
  /** 根拠のカードの、右のサイドパネルを開くボタン（3 製品で同じ名前。#2388）。 */
  sourceOpen: string;
  /** そのボタンの読み上げ（例:「文書 A.pdf の根拠を表示」）。 */
  sourceOpenLabel: (fileName: string) => string;
  /** 根拠のサイドパネルの見出し（回答の根拠。#2360）。 */
  sourceTitle: string;
  /** 回答を作らない検索の結果の箇所を開いたときの見出し（`EvidenceSourceSheet` の `kind="search"`）。 */
  sourceTitleSearch: string;
  sourceClose: string;
  /** パネルの要約の領域の名前（読み上げ）。 */
  sourceSummary: string;
  /** パネルの原文のプレビューの領域の名前（読み上げ）。 */
  sourcePreview: string;
  /** パネルの要約の、抜粋と頁・章節のチップの開閉の見出し（既定で閉じる。#2502）。 */
  excerptDetails: string;
  /** 抜粋の中の表の読み上げの名前。 */
  excerptTable: string;
}

/** 3 製品で同じ根拠の文言。 */
export function evidenceLabels(overrides: Partial<EvidenceLabels> = {}): EvidenceLabels {
  return {
    count: (count) => `AI に渡した ${count} 件`,
    used: (count) => `回答で引用 ${count} 件`,
    confirmed: (count) => `うち該当確認済み ${count} 件`,
    reference: (count) => `参考 ${count} 件`,
    context: (count) => `まとまりで参照 ${count} 件`,
    withheld: (count) => `権限の無い根拠 ${count} 件`,
    withheldNote: "今の権限では見られない（または削除された）資料の根拠です。抜粋・資料名・原文は表示しません。",
    separator: " ／ ",
    usedBadge: "回答で引用",
    referenceBadge: "参考",
    contextBadge: "まとまりで参照",
    superseded: "旧版",
    evidenceBadge: "Evidence",
    scoreRetrieval: "取得スコア",
    scoreRerank: "Rerank スコア",
    scoreRerankMissing: "Rerank 未実行",
    scoreMeter: (label, value) => `${label}: ${value}`,
    openSource: "原典を開く",
    citation: (fileName, location) => `根拠：${fileName}${location ? ` ${location}` : ""}`,
    citationOpen: (citation) => `${citation} を表示`,
    sourceOpen: "根拠を表示",
    sourceOpenLabel: (fileName) => `${fileName} の根拠を表示`,
    sourceTitle: "回答の根拠",
    sourceTitleSearch: "検索で見つかった箇所",
    sourceClose: "閉じる",
    sourceSummary: "根拠の要約",
    sourcePreview: "原文のプレビュー",
    excerptDetails: "抜粋と頁・章節",
    excerptTable: "抜粋の表",
    ...overrides,
  };
}

export const DEFAULT_EVIDENCE_LABELS: EvidenceLabels = evidenceLabels();

type CountItem = Pick<EvidenceListItem, "usedInAnswer" | "answerUse" | "usedAsContext">;

/**
 * 見出しの件数と内訳（「AI に渡した 45 件 ／ 回答で引用 13 件 ／ うち該当確認済み 10 件 ／ 参考 3 件」。3 製品で同じ）。
 * 回答がまとまりだけを参照した箇所は「回答で引用」に数えず、「まとまりで参照 N 件」として後ろに分ける（#2710）。
 * 引用したかが分からない（検索だけ）なら件数だけ、0 件の項目は出さない。
 */
export function evidenceCountLabel(
  items: readonly CountItem[],
  labels: EvidenceLabels = DEFAULT_EVIDENCE_LABELS,
  withheldCount = 0,
): string {
  // 権限の無い根拠も AI に渡した根拠なので「AI に渡した N 件」に含め、内訳の最後に数だけを出す（#3252）。
  const withheld = Math.max(0, Math.trunc(withheldCount));
  const parts = [labels.count(items.length + withheld)];
  if (items.some((item) => item.usedInAnswer !== null)) {
    const used = items.filter((item) => item.usedInAnswer === true);
    const confirmed = used.filter((item) => item.answerUse === "confirmed").length;
    const reference = used.filter((item) => item.answerUse === "reference").length;
    if (used.length) parts.push(labels.used(used.length));
    if (confirmed) parts.push(labels.confirmed(confirmed));
    if (reference) parts.push(labels.reference(reference));
    const context = items.filter((item) => item.usedInAnswer !== true && item.usedAsContext === true).length;
    if (context) parts.push(labels.context(context));
  }
  if (withheld) parts.push(labels.withheld(withheld));
  return parts.join(labels.separator);
}

/**
 * 保存した根拠のうち、見ている利用者が今の権限で見られない根拠の数の行（3 製品で同じ。#3252）。
 * 中身（抜粋・資料名・原文）は出さず、数と理由だけを出す。0 件なら何も出さない。注意を引く色にせず（利用者の
 * 誤りではない）、鍵のアイコンと文で示す。
 */
export function EvidenceWithheldNote({
  count,
  labels = DEFAULT_EVIDENCE_LABELS,
  className,
  "data-testid": testId = "evidence-withheld",
}: {
  count: number;
  labels?: EvidenceLabels;
  className?: string;
  "data-testid"?: string;
}) {
  const withheld = Math.max(0, Math.trunc(count));
  if (!withheld) return null;
  return (
    <p className={cn("flex min-w-0 items-start gap-2 text-sm text-fg-muted", className)} data-testid={testId}>
      <span className="pr-icon-line shrink-0">
        <Lock size={TEXT_ICON_SIZE.sm} aria-hidden="true" />
      </span>
      <span className="min-w-0 [overflow-wrap:anywhere]">
        <span className="font-medium text-fg">{labels.withheld(withheld)}</span>
        <span className="block text-xs">{labels.withheldNote}</span>
      </span>
    </p>
  );
}

/**
 * 回答で引用した箇所のバッジ。該当確認済みの記載は「回答で引用」（success）、該当が確認できていない記載は「参考」、
 * 使われ方が分からない引用は「回答で引用」（info）。色だけで分けず、文言とアイコンで区別する。
 */
export function EvidenceAnswerUseBadge({
  use,
  labels = DEFAULT_EVIDENCE_LABELS,
}: {
  use: EvidenceAnswerUse | null;
  labels?: EvidenceLabels;
}) {
  if (use === "reference") {
    return <StatusBadge variant="neutral" icon={BookOpen} label={labels.referenceBadge} />;
  }
  return <StatusBadge variant={use === "confirmed" ? "success" : "info"} label={labels.usedBadge} />;
}

export interface EvidenceCardProps {
  item: EvidenceListItem;
  /** 番号（0 始まり）。 */
  index: number;
  labels?: EvidenceLabels;
  /** カードの下の操作の行の左の組（「根拠を表示」。#2433）。 */
  leadingActions?: ReactNode;
  /**
   * カードの下の操作の行の右の組（根拠の評価など）。左右どちらも無ければ区切り線の行を出さない。
   */
  actions?: ReactNode;
  /** カードの最後に置くもの（操作が開くダイアログなど。表示の流れには入らない）。 */
  children?: ReactNode;
}

/** 根拠 1 件のカード（文書名・引用の印・経路・抜粋・場所のチップ・スコア・操作）。 */
export function EvidenceCard({
  item,
  index,
  labels = DEFAULT_EVIDENCE_LABELS,
  leadingActions,
  actions,
  children,
}: EvidenceCardProps) {
  return (
    <li className="rounded-lg border border-border bg-surface p-3" data-testid="evidence-card">
      <EvidenceSummaryBody item={item} index={index} labels={labels} excerpt="card" />
      <EvidenceActionRow>
        {leadingActions ? <EvidenceLeadingActions>{leadingActions}</EvidenceLeadingActions> : null}
        {actions}
      </EvidenceActionRow>
      {children}
    </li>
  );
}

/**
 * 操作の行（区切り線の下）。操作が 1 つも無いとき（閲覧だけの画面など）は、区切り線の行を出さない（中身のある
 * 要素が無ければ隠す）。
 */
function EvidenceActionRow({ children }: { children?: ReactNode }) {
  return (
    <div className="mt-2.5 flex flex-col gap-2 border-t border-border pt-2.5 not-has-[:not(:empty)]:hidden sm:flex-row sm:flex-wrap sm:items-center sm:justify-between">
      {children}
    </div>
  );
}

/**
 * 操作の行の左の組（「根拠を表示」）。右の組（根拠の評価など）とは行の両端に分かれる。中身が無ければ出さない。
 */
function EvidenceLeadingActions({ children }: { children?: ReactNode }) {
  return (
    <div className="flex flex-col gap-2 not-has-[:not(:empty)]:hidden sm:flex-row sm:flex-wrap sm:items-center">
      {children}
    </div>
  );
}

/**
 * 根拠 1 件の中身（カードとサイドパネルの要約で同じ描画。#2360）。`excerpt` は抜粋の出し方:
 * `card` は 3 行まで、`collapsed` は抜粋の全文と頁・章節のチップを「抜粋と頁・章節」の開閉に入れて既定で閉じる
 * （原文のプレビューに場所を空ける。#2502）、`full` は全文。
 */
function EvidenceSummaryBody({
  item,
  index,
  labels,
  excerpt,
}: {
  item: EvidenceListItem;
  index: number;
  labels: EvidenceLabels;
  excerpt: "card" | "collapsed" | "full";
}) {
  const badges = item.routeBadges ?? [];
  return (
    <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_176px] sm:items-start">
      <div data-testid="citation-main" className="min-w-0">
        <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1.5">
          <span
            data-surface-tint="accent"
            className="flex size-6 shrink-0 items-center justify-center rounded-full bg-accent-subtle text-xs font-semibold text-accent-fg"
          >
            {index + 1}
          </span>
          <span className="flex min-w-0 items-center gap-1.5 text-sm font-medium text-fg">
            <FileText size={14} className="shrink-0 text-fg-muted" aria-hidden />
            <span className="truncate" title={item.fileName} data-testid="evidence-file-name">
              {item.fileName}
            </span>
          </span>
          {item.superseded ? <StatusBadge variant="warning" icon={History} label={labels.superseded} /> : null}
          {item.usedInAnswer ? (
            <EvidenceAnswerUseBadge use={item.answerUse} labels={labels} />
          ) : item.usedAsContext ? (
            <StatusBadge variant="neutral" icon={Layers} label={labels.contextBadge} />
          ) : null}
          {badges.length ? (
            <span className="flex flex-wrap gap-1" data-testid="evidence-route-badges">
              {badges.map((badge) => (
                <span
                  key={badge}
                  className="rounded-full border border-border bg-surface-sunken px-1.5 py-0.5 text-xs font-medium leading-none text-fg-muted"
                >
                  {badge}
                </span>
              ))}
            </span>
          ) : null}
        </div>
        {excerpt === "card" ? (
          <>
            <p
              data-testid="citation-text"
              className="mt-2.5 line-clamp-3 whitespace-pre-wrap break-words text-sm leading-relaxed text-fg/90"
              lang={resolveContentLang(item.lang, item.text)}
            >
              {excerptPlainText(item.text)}
            </p>
            <EvidenceMetadata item={item} labels={labels} />
          </>
        ) : excerpt === "collapsed" ? (
          <Disclosure
            variant="plain"
            size="sm"
            summary={labels.excerptDetails}
            className="mt-2"
            contentClassName="pt-0"
            data-testid="evidence-excerpt-details"
            summaryProps={{ "data-testid": "evidence-excerpt-toggle" }}
          >
            <EvidenceExcerptScroll label={labels.excerptDetails}>
              <EvidenceExcerpt text={item.text} lang={item.lang} labels={labels} />
              <EvidenceMetadata item={item} labels={labels} />
            </EvidenceExcerptScroll>
          </Disclosure>
        ) : (
          <>
            <EvidenceExcerpt text={item.text} lang={item.lang} labels={labels} />
            <EvidenceMetadata item={item} labels={labels} />
          </>
        )}
      </div>
      <EvidenceScores score={item.score} rerankScore={item.rerankScore} labels={labels} />
    </div>
  );
}

/**
 * パネルの「抜粋と頁・章節」の中身の入れ物（#2512）。開いたときだけ描かれ、高さの上限（sm 以上はパネルの約 3 割・
 * sm 未満は約 2 割。要約の全体でパネルの 4 割ほど）を超えた分はこの中でスクロールする（下の原文のプレビューを
 * 押し出さない）。あふれている間は、Tab で届き（矢印キーで動かせる）、下端のフェードで続きがあることを示す。
 */
function EvidenceExcerptScroll({ label, children }: { label: string; children: ReactNode }) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [overflowing, setOverflowing] = useState(false);
  const [atEnd, setAtEnd] = useState(true);
  const measure = useCallback(() => {
    const element = scrollRef.current;
    if (!element) return;
    setOverflowing(element.scrollHeight > element.clientHeight + 1);
    setAtEnd(element.scrollTop + element.clientHeight >= element.scrollHeight - 1);
  }, []);
  useEffect(() => {
    measure();
    const element = scrollRef.current;
    if (!element || typeof ResizeObserver === "undefined") return undefined;
    const observer = new ResizeObserver(() => measure());
    observer.observe(element);
    if (element.firstElementChild) observer.observe(element.firstElementChild);
    return () => observer.disconnect();
  }, [measure]);
  return (
    <div
      ref={scrollRef}
      role="region"
      aria-label={label}
      tabIndex={overflowing ? 0 : undefined}
      onScroll={measure}
      className={cn(
        "relative max-h-[18dvh] overflow-y-auto pr-1 [scrollbar-gutter:stable] sm:max-h-[min(20rem,28dvh)]",
        INFORMATION_TABLE_FOCUS_CLASS
      )}
      data-overflowing={overflowing ? "true" : "false"}
      data-testid="evidence-excerpt-scroll"
    >
      <div>{children}</div>
      {/* 続きがあるときだけ、下端を面の色へ溶かす（読み上げには出さない）。 */}
      <div
        aria-hidden
        className={cn(
          "pointer-events-none sticky bottom-0 -mt-8 h-8 bg-gradient-to-t from-surface to-transparent transition-opacity",
          overflowing && !atEnd ? "opacity-100" : "opacity-0"
        )}
        data-testid="evidence-excerpt-fade"
      />
    </div>
  );
}

/** 根拠の頁・章節などのチップ・タグ・原典へのリンクの行。どれも無ければ出さない。 */
function EvidenceMetadata({ item, labels }: { item: EvidenceListItem; labels: EvidenceLabels }) {
  const chips = item.chips ?? [];
  const tags = item.tags ?? [];
  if (chips.length === 0 && tags.length === 0 && !item.sourceUrl) return null;
  return (
    <div className="mt-2 flex flex-wrap items-center gap-1.5">
      {chips.length > 0 ? (
        // 行（flex-wrap）の item の dl は min-width: auto だと、折り返さないチップの文字の全幅まで伸び、
        // チップの max-w-full と truncate が効かず根拠の一覧が横にスクロールする（375px。#3422）。行の幅で止める。
        <dl className="flex min-w-0 max-w-full flex-wrap gap-1.5" data-testid="evidence-chips">
          {chips.map((chip) => (
            <div
              key={chip.id}
              className="min-w-0 max-w-full rounded-full border border-border bg-surface-sunken px-2 py-0.5 text-xs text-fg-muted sm:max-w-80"
            >
              <dt className="sr-only">{chip.label}</dt>
              <dd className="truncate" title={chip.value}>
                {chip.value}
              </dd>
            </div>
          ))}
        </dl>
      ) : null}
      {tags.map((tag) => {
        const Icon = tag.icon;
        return tag.tone === "info" ? (
          <span
            key={tag.id}
            data-surface-tint="info"
            className="inline-flex rounded-full bg-info-subtle px-2 py-0.5 text-xs text-info-fg"
          >
            {tag.value}
          </span>
        ) : (
          <span
            key={tag.id}
            className="inline-flex items-center gap-1 rounded-full border border-border bg-surface-sunken px-2 py-0.5 text-xs text-fg-muted"
          >
            {Icon ? <Icon size={14} aria-hidden /> : null}
            {tag.value}
          </span>
        );
      })}
      {item.sourceUrl ? (
        <a
          href={item.sourceUrl}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-1 text-xs font-medium text-accent-fg hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring"
        >
          {labels.openSource}
          <ExternalLink size={14} aria-hidden />
        </a>
      ) : null}
    </div>
  );
}

/**
 * パネルの抜粋の全文。抜粋に入る文書の表（HTML の `<table>`）は表にする（HTML として注入しない。#2387）。
 */
function EvidenceExcerpt({ text, lang, labels }: { text: string; lang?: string | null; labels: EvidenceLabels }) {
  return (
    <div data-testid="citation-text" className="mt-2.5">
      <ExcerptContent text={text} lang={lang} className="text-sm leading-relaxed text-fg/90" tableLabel={labels.excerptTable} />
    </div>
  );
}

function EvidenceScores({
  score,
  rerankScore,
  labels,
}: {
  score: number | null;
  rerankScore: number | null;
  labels: EvidenceLabels;
}) {
  return (
    <div
      data-testid="citation-score-panel"
      className="w-full space-y-2 rounded-md bg-surface-sunken px-2.5 py-2 sm:w-[12.57rem] sm:shrink-0"
    >
      <div className="flex items-center justify-between gap-2 text-xs">
        <span className="font-medium text-fg-muted">{labels.scoreRetrieval}</span>
        <span className="tnum shrink-0 text-fg">{formatEvidenceScore(score)}</span>
      </div>
      <ScoreMeter label={labels.scoreRerank} value={rerankScore} labels={labels} />
    </div>
  );
}

function ScoreMeter({ label, value, labels }: { label: string; value: number | null; labels: EvidenceLabels }) {
  if (value == null || !Number.isFinite(value)) {
    return <p className="text-xs font-medium text-fg-muted">{labels.scoreRerankMissing}</p>;
  }
  const valueText = formatEvidenceScore(value);
  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between gap-2 text-xs">
        <span className="font-medium text-fg-muted">{label}</span>
        <span className="tnum shrink-0 text-fg">{valueText}</span>
      </div>
      <div
        data-surface-tint="neutral"
        role="meter"
        aria-label={labels.scoreMeter(label, valueText)}
        aria-valuemin={0}
        aria-valuemax={1}
        aria-valuenow={Math.min(Math.max(value, 0), 1)}
        aria-valuetext={valueText}
        className="h-1.5 overflow-hidden rounded-full bg-surface-hover"
      >
        <div
          data-testid="citation-rerank-fill"
          className="h-full rounded-full bg-success-emphasis"
          style={{ width: `${evidenceScoreMeterPercent(value)}%` }}
        />
      </div>
    </div>
  );
}

/** スコアの表示（小数 3 桁。分からなければ「—」）。 */
export function formatEvidenceScore(value: number | null): string {
  return value != null && Number.isFinite(value) ? value.toFixed(3) : "—";
}

/** Rerank のメーターの幅（0〜100%）。 */
export function evidenceScoreMeterPercent(value: number | null): number {
  if (value == null || !Number.isFinite(value) || value <= 0) return 0;
  return Math.min(100, value * 100);
}

/** 一覧の入れ物（一定の件数を超えたら一覧の中で縦にスクロールする。画面ごとに高さを書かない）。 */
export const EVIDENCE_LIST_CLASS = "bounded-scroll-area-lg space-y-2 pr-1";

export interface EvidenceListProps {
  items: readonly EvidenceListItem[];
  labels?: EvidenceLabels;
  /**
   * 根拠を右のサイドパネル（{@link EvidenceSourceSheet}）で開く。渡すと各カードの操作の行の先頭に
   * 「根拠を表示」（{@link EvidenceSourceButton}）を出す。開けない画面（文書を読む権限が無い等）では渡さない。
   */
  onOpenSource?: (index: number) => void;
  /** 根拠ごとの、操作の行の右の組の操作（根拠の評価など。{@link EvidenceCardProps.actions}）。 */
  renderActions?: (item: EvidenceListItem, index: number) => ReactNode;
  /** 根拠ごとの、カードの最後に置くもの（ダイアログなど）。 */
  renderExtra?: (item: EvidenceListItem, index: number) => ReactNode;
  /** 開くボタンの test id の接頭辞（`<idPrefix>-source-<番号>`）。同じ画面に複数の一覧があるときは分ける。 */
  idPrefix?: string;
  testId?: string;
}

/**
 * 根拠を右のサイドパネルで開くボタン（3 製品で同じ名前・アイコン・読み上げ。#2388）。根拠の一覧のカードは
 * {@link EvidenceList} が描く。1 件だけのカードなど、一覧の外で開くときに使う。
 */
export function EvidenceSourceButton({
  fileName,
  onOpen,
  labels = DEFAULT_EVIDENCE_LABELS,
  "data-testid": testId,
}: {
  fileName: string;
  onOpen: () => void;
  labels?: EvidenceLabels;
  "data-testid"?: string;
}) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      <Button
        type="button"
        variant="secondary"
        icon={LocateFixed}
        aria-haspopup="dialog"
        aria-label={labels.sourceOpenLabel(fileName)}
        onClick={onOpen}
        data-testid={testId}
      >
        {labels.sourceOpen}
      </Button>
    </div>
  );
}

/**
 * 根拠のカードの一覧（RAG のチャット・RAG 検索・実行の詳細、Agent のチャット・実行の詳細で同じ。#2328・#2388）。
 * 一定の件数を超えたら一覧の中で縦にスクロールする（回答の評価を押し下げない）。
 */
export function EvidenceList({
  items,
  labels = DEFAULT_EVIDENCE_LABELS,
  onOpenSource,
  renderActions,
  renderExtra,
  idPrefix = "evidence",
  testId,
}: EvidenceListProps) {
  return (
    <ul className={EVIDENCE_LIST_CLASS} data-testid={testId}>
      {items.map((item, index) => {
        const extraActions = renderActions?.(item, index);
        return (
          <EvidenceCard
            key={`${item.key}-${index}`}
            item={item}
            index={index}
            labels={labels}
            leadingActions={
              onOpenSource && !item.sourceUnavailable ? (
                <EvidenceSourceButton
                  fileName={item.fileName}
                  onOpen={() => onOpenSource(index)}
                  labels={labels}
                  data-testid={`${idPrefix}-source-${index}`}
                />
              ) : undefined
            }
            actions={extraActions}
          >
            {renderExtra?.(item, index)}
          </EvidenceCard>
        );
      })}
    </ul>
  );
}

export interface AnswerEvidenceProps extends Omit<EvidenceListProps, "testId"> {
  listTestId?: string;
  /** 保存した根拠のうち、見ている利用者が今の権限で見られない根拠の数（{@link EvidenceWithheldNote}。#3252）。 */
  withheldCount?: number;
  /** 開閉の受控（根拠の行から根拠を開くとき）。 */
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  className?: string;
  testId?: string;
}

/**
 * 回答の根拠の折りたたみ（既定は閉じる）。見出しは「AI に渡した N 件 ／ 回答で引用 M 件 …」で、回答の本文の下・
 * 回答の評価の前に置く（3 製品で同じ位置）。中身は {@link EvidenceList}（見方の切り替えは持たない。#2388）。
 * 根拠が無ければ何も出さない。
 */
export function AnswerEvidence({
  items,
  idPrefix = "answer",
  labels = DEFAULT_EVIDENCE_LABELS,
  onOpenSource,
  renderActions,
  renderExtra,
  listTestId,
  open,
  onOpenChange,
  className,
  withheldCount = 0,
  testId = "answer-citations",
}: AnswerEvidenceProps) {
  if (!items.length && withheldCount <= 0) return null;
  return (
    <Disclosure
      variant="plain"
      summary={evidenceCountLabel(items, labels, withheldCount)}
      className={cn(className)}
      open={open}
      onOpenChange={onOpenChange}
      data-testid={testId}
    >
      <div className="flex min-w-0 flex-col gap-2">
        <EvidenceWithheldNote count={withheldCount} labels={labels} />
        {items.length ? (
          <EvidenceList
            items={items}
            labels={labels}
            onOpenSource={onOpenSource}
            renderActions={renderActions}
            renderExtra={renderExtra}
            idPrefix={idPrefix}
            testId={listTestId}
          />
        ) : null}
      </div>
    </Disclosure>
  );
}

export interface EvidenceSourceSheetProps {
  open: boolean;
  /** 閉じる要求（×・Escape・scrim）。製品が `open` を false にする。 */
  onClose: () => void;
  /** 開く根拠。閉じる間（パネルが滑って消える間）も前の根拠を渡しておくと中身が消えない。 */
  item: EvidenceListItem | null;
  /** 根拠の番号（0 始まり。一覧・本文の根拠の行と同じ番号）。 */
  index: number;
  labels?: EvidenceLabels;
  /**
   * 開く元。`answer`（既定）は回答の根拠（見出し「回答の根拠」）、`search` は回答を作らない検索の結果の箇所
   * （見出し「検索で見つかった箇所」）。
   */
  kind?: "answer" | "search";
  /**
   * 要約の下に置く、製品の原文のプレビュー（RAG のページ画像のビューアなど）。渡すと抜粋と頁・章節のチップを
   * 「抜粋と頁・章節」の開閉に入れて既定で閉じ（開くたびに閉じた状態から。#2502）、プレビューにパネルの残りの高さを
   * 渡す。無ければ抜粋の全文を出す。
   */
  children?: ReactNode;
  /**
   * 要約の上に出す案内（info の `Banner`。翻訳済み）。本文の根拠の行が指す箇所を確定できないときなどに、
   * 開いた箇所が正確な位置ではないことを示す（#2710）。
   */
  notice?: { title: string; body?: string };
  returnFocusRef?: RefObject<HTMLElement | null>;
  "data-testid"?: string;
}

/**
 * 回答の根拠を右のサイドパネルで開く（RAG のチャット・RAG 検索・Agent のチャットと実行の詳細で共通。#2360）。
 *
 * 枠は {@link SideSheet}（右・`wide`。sm 未満は全画面のシート。×・Escape・scrim で閉じ、開いた操作へフォーカスを
 * 戻す）で、本文は左に残して見える。上部に根拠の要約（番号・文書名・引用の印・経路・抜粋・頁と章節・スコア）を
 * カードと同じ描画で置き、その下に製品の原文のプレビューを置く。本文の「根拠：…」の行と、根拠の一覧の
 * 「根拠を表示」は、どちらもこのパネルを開く。
 */
export function EvidenceSourceSheet({
  open,
  onClose,
  item,
  index,
  labels = DEFAULT_EVIDENCE_LABELS,
  kind = "answer",
  children,
  notice,
  returnFocusRef,
  "data-testid": testId = "evidence-source-sheet",
}: EvidenceSourceSheetProps) {
  const hasPreview = children != null && children !== false;
  // 開くたびに抜粋の開閉を閉じた状態へ戻す（同じ根拠を開き直したときも。#2502）。
  const [openCount, setOpenCount] = useState(open ? 1 : 0);
  const [wasOpen, setWasOpen] = useState(open);
  if (open !== wasOpen) {
    setWasOpen(open);
    if (open) setOpenCount((count) => count + 1);
  }
  const heading = kind === "search" ? labels.sourceTitleSearch : labels.sourceTitle;
  return (
    <SideSheet
      open={open}
      onClose={onClose}
      title={heading}
      closeLabel={labels.sourceClose}
      side="right"
      size="wide"
      returnFocusRef={returnFocusRef}
      // 読むためのパネルなので、開いたらパネルそのものへフォーカスする（キーボードで開いた直後の Escape 1 回で閉じる）。
      initialFocus="dialog"
      bodyClassName="gap-3"
      data-testid={testId}
    >
      {item ? (
        <>
          {notice ? (
            <div className="shrink-0" data-testid="evidence-source-notice">
              <Banner severity="info" title={notice.title}>
                {notice.body}
              </Banner>
            </div>
          ) : null}
          <section
            aria-label={labels.sourceSummary}
            className="shrink-0 rounded-lg border border-border bg-surface p-3"
            data-testid="evidence-source-summary"
          >
            <EvidenceSummaryBody
              key={`${openCount}:${item.key}`}
              item={item}
              index={index}
              labels={labels}
              excerpt={hasPreview ? "collapsed" : "full"}
            />
          </section>
          {hasPreview ? (
            <section
              aria-label={labels.sourcePreview}
              // 抜粋を開いても押し出されないよう、下限は画面の高さに合わせて小さくする（残りの高さはすべて使う。#2512）。
              className="flex min-h-[min(24rem,45dvh)] flex-1 flex-col sm:min-h-[min(32rem,55dvh)]"
              data-testid="evidence-source-preview"
            >
              {children}
            </section>
          ) : null}
        </>
      ) : children}
    </SideSheet>
  );
}

/** {@link useEvidenceSource} の状態。 */
export interface EvidenceSourceState<T> {
  /** 開いている根拠（閉じていれば null）。 */
  selected: T | null;
  /** パネルに出す根拠。閉じた後も前の根拠を覚え、パネルが滑って消える間も中身を出し続ける。 */
  shown: T | null;
  /** 一度でも開いたか（開くまでパネルを描かない。閉じた後も描いたままにし、フォーカスを開いた操作へ戻す）。 */
  mounted: boolean;
  open: (value: T) => void;
  close: () => void;
}

/** 根拠のサイドパネルで開いている根拠の状態（#2360。3 製品で同じ開き方・閉じ方にする）。 */
export function useEvidenceSource<T>(): EvidenceSourceState<T> {
  const [selected, setSelected] = useState<T | null>(null);
  const [shown, setShown] = useState<T | null>(null);
  return {
    selected,
    shown,
    mounted: shown !== null,
    open: (value) => {
      setSelected(value);
      setShown(value);
    },
    close: () => setSelected(null),
  };
}
