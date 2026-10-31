import { ChevronDown, ChevronUp, FileText } from "lucide-react";
import { useId, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { detectContentLang, resolveContentLang } from "../../lib/content-lang";
import { cn } from "../../lib/utils";
import { Button } from "../ui/button";
import { TEXT_ICON_SIZE } from "../ui/text-icon";
import { Tooltip } from "../ui/tooltip";
import { AnswerHeading } from "./answer-heading";
import {
  ANSWER_PASSAGE_STATE_ICON,
  AnswerPassageMarked,
  AnswerPassageWarnings,
  DEFAULT_ANSWER_PASSAGE_LABELS,
  answerPassageQuote,
  answerPassageReasonText,
  focusAnswerPassage,
  type AnswerPassageLabels,
} from "./answer-passage-marks";
import {
  matchAnswerPassageMarks,
  visibleAnswerPassageRecords,
  type AnswerPassageMarkGroup,
  type AnswerPassageRecords,
} from "./answer-passage-records";

/**
 * 回答の本文の節と根拠の行（RAG のチャット・RAG 検索・Agent のチャットで共通。#2328）。
 *
 * 回答を「要約の段落 →『資料により確認できた事項』の箇条書き（各項目の下に『根拠：文書 p.N』）→
 * 『資料から確認できなかった事項』」のように節に分けて出す。本文の文は製品が渡し（書き換えない）、
 * ここは並べ方と根拠の行の見た目だけを持つ。
 *
 * 項目の多い節（`collapsible`。資料から確認できなかった事項など）は、先頭の
 * `ANSWER_SECTION_COLLAPSE_AFTER` 件だけを出し、「すべて表示（全 N 件）」で残りを同じ書式のまま開く（#2361。
 * 「ほか N 件」の行で切らず、同じ種類の項目を 2 か所に分けない）。
 *
 * 段落の印の記録（`passages`。#2738）を渡すと、印の付いた段落・項目に左の区切りの線と末尾の印を付け、
 * 本文の上に回答全体の注意、本文の後ろに「根拠と矛盾し除いた内容」「確認が必要な点」の節を足す。段落の照合は
 * 段落・項目の原文（`passageText`。無ければ文字列の `content`）と印の `text` で行う。
 *
 * 回答の言語の `lang`（`lang`。渡さなければ本文から判定する。#3335）は、段落・項目の文・表やコードの塊・引用だけを包む
 * 要素に付ける。部品と製品の文言（節の見出し・根拠の行・ボタン・段落の印・注意・`extra`）は包まず、画面の言語
 * （`<html lang>`）のまま描く（中国語の回答の中の日本語の文言を中国語の字形にしない。#3349）。節の見出しは見出しの文字から
 * 判定した言語にする（判定できなければ画面の言語）。製品の決まった文言の段落・項目は `lang: null` で除く。
 */

/** 畳める節で、畳んだときに出す項目の数。 */
export const ANSWER_SECTION_COLLAPSE_AFTER = 10;

export interface AnswerSectionsLabels {
  /** 畳んだ節を開くボタン（`{count}` は節の項目の数）。 */
  showAll: (count: number) => string;
  /** 開いた節を畳むボタン。 */
  collapse: string;
}

export const DEFAULT_ANSWER_SECTIONS_LABELS: AnswerSectionsLabels = {
  showAll: (count) => `すべて表示（全 ${count} 件）`,
  collapse: "折りたたむ",
};

/** 根拠の行（「根拠：文書 p.3」）。`onOpen` があれば押せる行（原文・根拠を開く）、無ければ文字だけ。 */
export interface AnswerCitationRef {
  key: string;
  label: string;
  /** 押せる行の読み上げの名前（例:「根拠：文書 p.3 を表示」）。 */
  ariaLabel?: string;
  /**
   * `label` の末尾の位置（例:「p.3」）。渡すと、押せる行の長い名前を省略しても位置は省略せずに出す（#3353）。
   * `label` の末尾と一致しないときは使わない。
   */
  location?: string;
  onOpen?: () => void;
}

export interface AnswerSectionEntry {
  key: string;
  content: ReactNode;
  /** 箇条書きの印を付けない補足の行（節の説明など）。 */
  muted?: boolean;
  citations?: readonly AnswerCitationRef[];
  /** 根拠の行の後ろに足すもの（章節を開くリンクなど）。 */
  extra?: ReactNode;
  /** 段落の印の照合に使う原文（無ければ文字列の `content`）。 */
  passageText?: string;
  /**
   * 項目の文の言語（#3349）。渡さなければ回答の言語、文字列ならその言語、`null` なら `lang` を付けない
   * （製品の決まった説明の文など、画面の言語で描く文）。
   */
  lang?: string | null;
}

export type AnswerSectionsBlock =
  | {
      kind: "paragraph";
      key: string;
      /** 段落の文（文字列は改行を保って出す）。 */
      content: ReactNode;
      citations?: readonly AnswerCitationRef[];
      /** 段落の印の照合に使う原文（無ければ文字列の `content`）。 */
      passageText?: string;
      /** 段落の言語（`AnswerSectionEntry.lang` と同じ。`null` は製品の決まった文言）。 */
      lang?: string | null;
    }
  | {
      /** 段落にできない本文（表・コード・見出しなど。製品が描いたもの）と、その下の根拠の行。 */
      kind: "content";
      key: string;
      content: ReactNode;
      citations?: readonly AnswerCitationRef[];
      /** 段落の印の照合に使う原文。 */
      passageText?: string;
      /** 塊の言語（`AnswerSectionEntry.lang` と同じ）。 */
      lang?: string | null;
    }
  | {
      kind: "section";
      key: string;
      /** 節の見出し。無ければ見出しなしの箇条書き。 */
      title?: string;
      ordered?: boolean;
      entries: readonly AnswerSectionEntry[];
      /** 項目が多いとき（`ANSWER_SECTION_COLLAPSE_AFTER` 件を超える）、先頭だけを出して残りを畳む。 */
      collapsible?: boolean;
      /** 節の data-testid（既定 `answer-section`）。 */
      testId?: string;
    };

/** 根拠の行の文を、省略してよい名前と省略しない末尾の位置に分ける。 */
function splitAnswerCitationLabel(label: string, location?: string): { head: string; location: string | null } {
  const tail = location?.trim();
  if (!tail || !label.endsWith(tail)) return { head: label, location: null };
  const head = label.slice(0, -tail.length).trimEnd();
  // 区切りの空白は位置の側に残す（読み上げ・文字の取得で「文書 A.pdf p.3」のまま。見た目の間は Button の gap）。
  return head ? { head, location: label.slice(head.length) } : { head: label, location: null };
}

/**
 * 押せる根拠の行（#3353）。長いファイル名は 1 行に収めて名前の末尾を省略し（`Button` の `[&>span]:truncate`）、位置
 * （`location`。頁など）は省略しない。省略したときだけ全文を Tooltip で出す。読み上げの名前（`ariaLabel`）は全文のまま。
 */
function AnswerCitationLinkButton({ citation }: { citation: AnswerCitationRef }) {
  const { head, location } = splitAnswerCitationLabel(citation.label, citation.location);
  const labelRef = useRef<HTMLSpanElement | null>(null);
  const [truncated, setTruncated] = useState(false);
  useLayoutEffect(() => {
    const element = labelRef.current;
    if (!element) return undefined;
    const measure = () => setTruncated(element.scrollWidth - element.clientWidth > 1);
    measure();
    if (typeof ResizeObserver === "undefined") return undefined;
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [head]);
  return (
    // 行の幅に収める（`min-w-0`）。本文の入れ物の最小幅を根拠の行の全長で広げない。
    <span className="mt-0.5 flex min-w-0">
      <Tooltip content={citation.label} disabled={!truncated} describe={false}>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          icon={FileText}
          // 押せる根拠の行は強調色にし、当たらない根拠の行（文字だけ）と見分ける。
          className="text-accent-fg"
          aria-label={citation.ariaLabel}
          onClick={citation.onOpen}
          data-testid="answer-citation-link"
          data-truncated={truncated || undefined}
        >
          <span ref={labelRef}>{head}</span>
          {location ? <span className="shrink-0">{location}</span> : null}
        </Button>
      </Tooltip>
    </span>
  );
}

/** 根拠の行の並び。 */
export function AnswerCitationLinks({ citations }: { citations: readonly AnswerCitationRef[] }) {
  return (
    <>
      {citations.map((citation) =>
        citation.onOpen ? (
          <AnswerCitationLinkButton key={citation.key} citation={citation} />
        ) : (
          <span
            key={citation.key}
            className="mt-0.5 flex min-w-0 items-center gap-1 text-xs text-fg-muted"
            data-testid="answer-citation-text"
          >
            <FileText size={14} aria-hidden className="shrink-0" />
            {/* 押せない行は省略せず折り返す（全文を読める）。 */}
            <span className="min-w-0 [overflow-wrap:anywhere]">{citation.label}</span>
          </span>
        ),
      )}
    </>
  );
}

export interface AnswerSectionsProps {
  blocks: readonly AnswerSectionsBlock[];
  /** 本文の更新を読み上げる（チャット）。 */
  live?: boolean;
  /** 末尾に置くもの（根拠を開くダイアログなど）。 */
  children?: ReactNode;
  className?: string;
  testId?: string;
  labels?: AnswerSectionsLabels;
  /**
   * 段落の印・外した段落・回答全体の注意（#2738。`parseAnswerPassageRecords` で読んだもの）。新しい記録の無い
   * 過去の回答は null（今までの表示のまま）。
   */
  passages?: AnswerPassageRecords | null;
  passageLabels?: AnswerPassageLabels;
  /**
   * 回答の言語（BCP 47。#3335。`resolveContentLang`）。渡さなければ段落・項目の文（文字列の `content` と
   * `passageText`）から判定して段落・項目の文に `lang` を付け、`null` なら付けない。
   */
  lang?: string | null;
}

/** 印を当てた段落（照合の結果と、移る先の id）。 */
interface MarkedTarget {
  group: AnswerPassageMarkGroup;
  anchorId: string;
}

type MarkedTargets = ReadonlyMap<string, MarkedTarget>;

const NO_MARKS: MarkedTargets = new Map();

function entryTargetKey(blockKey: string, entryKey: string): string {
  return `${blockKey}/${entryKey}`;
}

function passageTextOf(passageText: string | undefined, content: ReactNode): string | undefined {
  if (passageText !== undefined) return passageText;
  return typeof content === "string" ? content : undefined;
}

export interface AnswerPlainTextProps {
  /** 回答の本文（節に分けない文字列。改行はそのまま出す）。 */
  text: string;
  /** 本文の更新を読み上げる（チャット）。 */
  live?: boolean;
  /** 回答の言語（BCP 47。`resolveContentLang`）。渡さなければ本文から判定し、`null` なら `lang` を付けない。 */
  lang?: string | null;
  /** 本文の後ろに置くもの（生成中のカーソルなど）。 */
  children?: ReactNode;
  className?: string;
  testId?: string;
}

/**
 * 節に分けない回答の本文（生成中・回答エンジンの書式に合わない本文。#3335）。`AnswerSections` と同じ文字の大きさ・
 * 行の高さで、本文だけを回答の言語の `lang` で包む（中国語の回答は中国語の字形で描く。後ろに置く `children` は画面の
 * 言語のまま。#3349）。
 */
export function AnswerPlainText({ text, live = false, lang, children, className, testId }: AnswerPlainTextProps) {
  const contentLang = useMemo(() => resolveContentLang(lang, text), [lang, text]);
  // 後ろに置くものが無ければ段落が本文だけなので段落に付け、あれば本文だけを包む。
  const trailing = children !== undefined && children !== null && children !== false;
  return (
    <p
      className={cn("whitespace-pre-wrap text-sm leading-relaxed text-fg", className)}
      aria-live={live ? "polite" : undefined}
      lang={trailing ? undefined : contentLang}
      data-testid={testId}
    >
      {trailing ? <ContentLang lang={contentLang}>{text}</ContentLang> : text}
      {children}
    </p>
  );
}

/** 言語の判定に使う回答の文（段落・項目の原文。製品の決まった文言 `lang: null` と節の見出しは入れない）。 */
function answerText(blocks: readonly AnswerSectionsBlock[]): string {
  const parts: string[] = [];
  for (const block of blocks) {
    if (block.kind === "section") {
      for (const entry of block.entries) {
        const text = entry.lang === null ? undefined : passageTextOf(entry.passageText, entry.content);
        if (text) parts.push(text);
      }
    } else {
      const text = block.lang === null ? undefined : passageTextOf(block.passageText, block.content);
      if (text) parts.push(text);
    }
  }
  return parts.join("\n");
}

/** 段落・項目の言語（`lang` を渡さなければ回答の言語、`null` は付けない）。 */
function partLang(lang: string | null | undefined, answerLang: string | undefined): string | undefined {
  if (lang === undefined) return answerLang;
  return resolveContentLang(lang, null);
}

/** 利用者の内容だけを包む行内の要素（言語が無ければ包まない）。 */
function ContentLang({ lang, children }: { lang: string | undefined; children: ReactNode }) {
  return lang ? <span lang={lang}>{children}</span> : <>{children}</>;
}

/** 回答の本文の節（段落・見出しつきの箇条書き・各項目の根拠の行）。 */
export function AnswerSections({
  blocks,
  live = false,
  children,
  className,
  testId = "answer-text",
  labels = DEFAULT_ANSWER_SECTIONS_LABELS,
  passages: rawPassages = null,
  passageLabels = DEFAULT_ANSWER_PASSAGE_LABELS,
  lang,
}: AnswerSectionsProps) {
  const passages = useMemo(() => rawPassages ? visibleAnswerPassageRecords(rawPassages) : null, [rawPassages]);
  const baseId = useId();
  // 回答の本文の言語（中国語の回答は中国語の字形で描く。#3335）。
  const contentLang = useMemo(() => resolveContentLang(lang, answerText(blocks)), [lang, blocks]);
  // 本文の段落・項目（表示の順）に印を当てる。
  const { marked, anchors } = useMemo(() => {
    if (!passages?.marks.length) return { marked: NO_MARKS, anchors: [] as string[] };
    const keys: string[] = [];
    const texts: (string | undefined)[] = [];
    for (const block of blocks) {
      if (block.kind === "section") {
        // 補足の行（`muted`）も本文の段落なので照合する（RAG の節の後ろに続く段落など）。
        for (const entry of block.entries) {
          keys.push(entryTargetKey(block.key, entry.key));
          texts.push(passageTextOf(entry.passageText, entry.content));
        }
      } else {
        keys.push(block.key);
        texts.push(passageTextOf(block.passageText, block.content));
      }
    }
    const { groups, targets } = matchAnswerPassageMarks(texts, passages.marks);
    const map = new Map<string, MarkedTarget>();
    groups.forEach((group, index) => {
      if (group) map.set(keys[index], { group, anchorId: `${baseId}-passage-${index}` });
    });
    return {
      marked: map as MarkedTargets,
      anchors: targets.map((index) => (index < 0 ? "" : `${baseId}-passage-${index}`)),
    };
  }, [blocks, passages, baseId]);

  const extraSections = passages ? passageSections(passages, anchors, passageLabels, contentLang) : [];
  return (
    <div
      className={cn("space-y-3 text-sm leading-relaxed text-fg", className)}
      aria-live={live ? "polite" : undefined}
      data-testid={testId}
    >
      {passages?.warnings.length ? <AnswerPassageWarnings warnings={passages.warnings} labels={passageLabels} /> : null}
      {blocks.map((block) => {
        if (block.kind === "section") {
          // 節の見出しの前は、段落の間（0.75rem）より広い 1.25rem（`--space-5`。0.75rem + pt-2）を空け、節の区切りを
          // 見て分かるようにする（#2325。見出しは後ろの内容に近く、前の内容から離す）。先頭の節には付けない。
          return (
            <AnswerSection
              key={block.key}
              block={block}
              labels={labels}
              marked={marked}
              passageLabels={passageLabels}
              answerLang={contentLang}
            />
          );
        }
        const blockLang = partLang(block.lang, contentLang);
        const target = marked.get(block.key);
        const citations = block.citations?.length ? <AnswerCitationLinks citations={block.citations} /> : null;
        if (target) {
          return (
            <AnswerPassageMarked
              key={block.key}
              group={target.group}
              anchorId={target.anchorId}
              labels={passageLabels}
              render={(marker) =>
                block.kind === "content" ? (
                  <>
                    <div className="min-w-0" lang={blockLang}>
                      {block.content}
                    </div>
                    {/* 表・コードの後ろには文が続かないので、印は塊の下の行に置く。 */}
                    <p>{marker}</p>
                    {citations}
                  </>
                ) : (
                  <>
                    <p className="whitespace-pre-wrap">
                      <ContentLang lang={blockLang}>{block.content}</ContentLang>
                      {marker}
                    </p>
                    {citations}
                  </>
                )
              }
            />
          );
        }
        if (block.kind === "content") {
          return (
            <div key={block.key} className="min-w-0">
              <div className="min-w-0" lang={blockLang}>
                {block.content}
              </div>
              {citations}
            </div>
          );
        }
        return citations ? (
          <div key={block.key}>
            <p className="whitespace-pre-wrap" lang={blockLang}>
              {block.content}
            </p>
            {citations}
          </div>
        ) : (
          <p key={block.key} className="whitespace-pre-wrap" lang={blockLang}>
            {block.content}
          </p>
        );
      })}
      {extraSections.map((block) => (
        <AnswerSection
          key={block.key}
          block={block}
          labels={labels}
          marked={NO_MARKS}
          passageLabels={passageLabels}
          answerLang={contentLang}
        />
      ))}
      {children}
    </div>
  );
}

/**
 * 本文の後ろの節（#2738）: 「根拠と矛盾し除いた内容」（外した段落と理由）と「確認が必要な点」（印の付いた段落の
 * 引用と理由。押すと本文の段落へ移る）。どちらも項目が多ければ畳む。
 */
function passageSections(
  passages: AnswerPassageRecords,
  anchors: readonly string[],
  labels: AnswerPassageLabels,
  answerLang: string | undefined,
): Extract<AnswerSectionsBlock, { kind: "section" }>[] {
  // 節の見出し・説明・理由は部品の文言（画面の言語）。引用だけが回答の言語（#3349）。
  const sections: Extract<AnswerSectionsBlock, { kind: "section" }>[] = [];
  if (passages.removed.length) {
    sections.push({
      kind: "section",
      key: "passage-removed",
      title: labels.removedTitle,
      collapsible: true,
      testId: "answer-removed-passages",
      entries: [
        { key: "note", content: labels.removedNote, muted: true, lang: null },
        ...passages.removed.map((item, index) => ({
          key: `removed-${index}`,
          lang: null,
          content: (
            <span className="block whitespace-normal" data-testid="answer-removed-passage">
              <span className="[overflow-wrap:anywhere]" lang={answerLang}>
                {answerPassageQuote(item.text, 200)}
              </span>
              {item.reasons.length ? (
                <span className="block text-xs text-fg-muted [overflow-wrap:anywhere]">
                  {item.reasons.map(answerPassageReasonText).join(" / ")}
                </span>
              ) : null}
            </span>
          ),
        })),
      ],
    });
  }
  if (passages.marks.length) {
    sections.push({
      kind: "section",
      key: "passage-marks",
      title: labels.marksTitle,
      collapsible: true,
      testId: "answer-marked-passages",
      entries: [
        { key: "note", content: labels.marksNote, muted: true, lang: null },
        ...passages.marks.map((mark, index) => {
          const Icon = ANSWER_PASSAGE_STATE_ICON[mark.state];
          const quote = answerPassageQuote(mark.text);
          const anchor = anchors[index];
          const reasons = [...new Set(mark.reasons.map((reason) => reason.label))].join("・");
          return {
            key: `mark-${index}`,
            lang: null,
            content: (
              <span className="block whitespace-normal" data-testid="answer-marked-passage" data-passage-state={mark.state}>
                {anchor ? (
                  <button
                    type="button"
                    // button は inline でも inline-block として並び、折り返すと最後の行の基線にそろう。align-top で箇条書きの印を 1 行目にそろえる。
                    className="pr-touch-target relative rounded-sm text-left align-top text-accent-fg underline-offset-2 hover:underline [overflow-wrap:anywhere]"
                    aria-label={labels.jumpLabel(quote)}
                    onClick={() => focusAnswerPassage(anchor)}
                    data-testid="answer-marked-passage-link"
                  >
                    <ContentLang lang={answerLang}>{quote}</ContentLang>
                  </button>
                ) : (
                  <span className="[overflow-wrap:anywhere]" lang={answerLang}>
                    {quote}
                  </span>
                )}
                <span className="flex items-start gap-1 text-xs text-fg-muted">
                  <span className="pr-icon-line">
                    <Icon size={TEXT_ICON_SIZE.xs} aria-hidden />
                  </span>
                  <span className="min-w-0 [overflow-wrap:anywhere]">
                    {labels.state[mark.state]}
                    {reasons ? `：${reasons}` : ""}
                  </span>
                </span>
              </span>
            ),
          };
        }),
      ],
    });
  }
  return sections;
}

function AnswerSection({
  block,
  labels,
  marked,
  passageLabels,
  answerLang,
}: {
  block: Extract<AnswerSectionsBlock, { kind: "section" }>;
  labels: AnswerSectionsLabels;
  marked: MarkedTargets;
  passageLabels: AnswerPassageLabels;
  answerLang: string | undefined;
}) {
  const [expanded, setExpanded] = useState(false);
  const listId = useId();
  // 数えるのは箇条書きの項目だけ（節の説明の行 `muted` は畳まずに出す）。
  const items = block.entries.filter((entry) => !entry.muted).length;
  const toggle = Boolean(block.collapsible) && items > ANSWER_SECTION_COLLAPSE_AFTER;
  const entries = toggle && !expanded ? firstItems(block.entries, ANSWER_SECTION_COLLAPSE_AFTER) : block.entries;
  const render = (entry: AnswerSectionEntry) =>
    renderEntry(entry, marked.get(entryTargetKey(block.key, entry.key)), passageLabels, partLang(entry.lang, answerLang));
  // 見出しは製品の決まった見出しのことも回答の見出しのこともあるので、見出しの文字だけで決める（判定できなければ画面の
  // 言語。#3349）。
  const titleLang = useMemo(() => detectContentLang(block.title), [block.title]);
  return (
    <section className="space-y-1.5 not-first:pt-2" data-testid={block.testId ?? "answer-section"}>
      {block.title ? (
        // 節の見出しの書式はチャットの回答の見出し「回答」と同じ部品（#3433）。
        <AnswerHeading level={4} lang={titleLang}>
          {block.title}
        </AnswerHeading>
      ) : null}
      {block.ordered ? (
        <ol id={listId} className="list-decimal space-y-1.5 pl-5">
          {entries.map(render)}
        </ol>
      ) : (
        <ul id={listId} className="list-disc space-y-1.5 pl-5">
          {entries.map(render)}
        </ul>
      )}
      {toggle ? (
        <Button
          type="button"
          variant="ghost"
          size="sm"
          icon={expanded ? ChevronUp : ChevronDown}
          aria-expanded={expanded}
          aria-controls={listId}
          onClick={() => setExpanded((value) => !value)}
          data-testid="answer-section-toggle"
        >
          {expanded ? labels.collapse : labels.showAll(items)}
        </Button>
      ) : null}
    </section>
  );
}

/** 先頭から `limit` 件の項目までの行（節の説明の行 `muted` は数えずに残す）。 */
function firstItems(entries: readonly AnswerSectionEntry[], limit: number): AnswerSectionEntry[] {
  const result: AnswerSectionEntry[] = [];
  let items = 0;
  for (const entry of entries) {
    if (!entry.muted) {
      if (items >= limit) break;
      items += 1;
    }
    result.push(entry);
  }
  return result;
}

function renderEntry(
  entry: AnswerSectionEntry,
  target: MarkedTarget | undefined,
  passageLabels: AnswerPassageLabels,
  entryLang: string | undefined,
) {
  // 項目の文だけを言語で包み、根拠の行・`extra`・段落の印は画面の言語のまま（#3349）。
  if (entry.muted) {
    return (
      <li key={entry.key} className="list-none whitespace-pre-wrap text-fg-muted">
        {target ? (
          <AnswerPassageMarked
            group={target.group}
            anchorId={target.anchorId}
            labels={passageLabels}
            render={(marker) => (
              <span>
                <ContentLang lang={entryLang}>{entry.content}</ContentLang>
                {marker}
              </span>
            )}
          />
        ) : (
          <ContentLang lang={entryLang}>{entry.content}</ContentLang>
        )}
      </li>
    );
  }
  const citations = entry.citations?.length ? <AnswerCitationLinks citations={entry.citations} /> : null;
  if (target) {
    return (
      <li key={entry.key} className="whitespace-pre-wrap">
        <AnswerPassageMarked
          group={target.group}
          anchorId={target.anchorId}
          labels={passageLabels}
          render={(marker) => (
            <>
              <span>
                <ContentLang lang={entryLang}>{entry.content}</ContentLang>
                {marker}
              </span>
              {citations}
              {entry.extra}
            </>
          )}
        />
      </li>
    );
  }
  return (
    <li key={entry.key} className="whitespace-pre-wrap">
      <ContentLang lang={entryLang}>{entry.content}</ContentLang>
      {citations}
      {entry.extra}
    </li>
  );
}
