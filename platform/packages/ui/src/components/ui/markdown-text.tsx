import { Fragment, useMemo, type ReactNode } from "react";

import { resolveContentLang, uiTextLang } from "../../lib/content-lang";
import {
  parseMarkdownBlocks,
  parseMarkdownInline,
  type MarkdownBlock,
  type MarkdownInline,
} from "../../lib/markdown";
import { cn } from "../../lib/utils";

export interface MarkdownTextLabels {
  /** 新しいタブで開くリンクの読み上げの補足（画面には出さない）。 */
  newTab: string;
}

const DEFAULT_LABELS: MarkdownTextLabels = {
  newTab: "（新しいタブで開きます）",
};

export interface MarkdownTextProps {
  /** Markdown の本文（LLM の回答など）。表は扱わない（呼び出し側が `splitMarkdownTables` で先に取り出す）。 */
  text: string;
  /**
   * 本文の最上位の見出しに使う HTML の見出しの段（既定 3）。置く場所の見出し（`Section` の h2 など）の下になる段を渡す。
   * 本文の `#` の数はそのまま使わず、本文の中で最も上の見出しをこの段にして、下の見出しを 1 段ずつ下げる（最大 h6）。
   */
  headingLevel?: 2 | 3 | 4 | 5 | 6;
  labels?: Partial<MarkdownTextLabels>;
  /**
   * 本文の言語（#3335。`resolveContentLang`）。渡さなければ本文から判定して `lang` を付け、`null` なら付けない。
   * 取込などで言語が分かっていれば、その値（BCP 47）を渡す。
   */
  lang?: string | null;
  className?: string;
  testId?: string;
}

/**
 * Markdown の本文（#1808）。LLM の回答などの見出し・段落・太字・斜体・箇条書き（入れ子）・番号付き・
 * インラインコード・コードブロック・リンク・引用・区切り線を、React の要素だけで描く（3 製品で同じ描き方）。
 *
 * - 生の HTML は解釈せず文字のまま出す（`dangerouslySetInnerHTML` を使わない。XSS を防ぐ）。
 * - リンクは `http(s)` / `mailto` だけ。`http(s)` は新しいタブ（`rel="noopener noreferrer"`）で開き、読み上げで
 *   新しいタブで開くことを伝える。ほかの scheme（`javascript:` など）・相対の URL はリンクにせず文字のまま。
 * - 段落の中の改行はそのまま改行にする（日本語の回答は改行を区切りに使うため、空白にまとめない）。
 * - 長いコードは枠の中で横にスクロールし（キーボードでもスクロールできるようフォーカスを受ける）、長い URL・語は
 *   折り返して、375px でもページを横にはみ出さない。色・余白・コードの地はトークン。
 * - 本文の言語を `lang` に付け、漢字を内容の言語の字形で描く（中国語の回答は Noto Sans SC など。#3335）。リンクの
 *   読み上げの補足（部品の文言）は画面の言語（#3349）。
 */
export function MarkdownText({ text, headingLevel = 3, labels, lang, className, testId }: MarkdownTextProps) {
  const contentLang = useMemo(() => resolveContentLang(lang, text), [lang, text]);
  const blocks = parseMarkdownBlocks(text);
  const levels = collectHeadingLevels(blocks);
  const context: RenderContext = {
    topLevel: levels.length ? Math.min(...levels) : 1,
    headingLevel,
    labels: { ...DEFAULT_LABELS, ...labels },
  };
  if (blocks.length === 0) return null;
  return (
    <div
      className={cn(
        // 文の中は文節で折り返し、長い URL・語は枠の中で折り返す（`MessageText` と同じ `.pr-message-text`。#899）。
        "pr-message-text min-w-0 space-y-3 text-sm leading-6 text-fg",
        className,
      )}
      data-markdown-text
      lang={contentLang}
      data-testid={testId}
    >
      {renderBlocks(blocks, context)}
    </div>
  );
}

/**
 * 1 つの段落・箇条書きの項目の中の Markdown（太字・斜体・インラインコード・リンク）だけを描く（#2328）。
 * 段落の要素（`<p>` / `<li>`）の中に置く。改行は `<br>` にする。ブロック（見出し・箇条書き・表）は扱わない。
 */
export function MarkdownInlineText({ text, labels }: { text: string; labels?: Partial<MarkdownTextLabels> }) {
  const context: RenderContext = { topLevel: 1, headingLevel: 3, labels: { ...DEFAULT_LABELS, ...labels } };
  return <Lines lines={text.split("\n")} context={context} />;
}

interface RenderContext {
  topLevel: number;
  headingLevel: number;
  labels: MarkdownTextLabels;
}

function collectHeadingLevels(blocks: MarkdownBlock[]): number[] {
  return blocks.flatMap((block) => {
    if (block.kind === "heading") return [block.level];
    if (block.kind === "list") return block.items.flatMap(collectHeadingLevels);
    if (block.kind === "quote") return collectHeadingLevels(block.blocks);
    return [];
  });
}

function renderBlocks(blocks: MarkdownBlock[], context: RenderContext): ReactNode {
  return blocks.map((block, index) => <Block key={`${block.kind}-${index}`} block={block} context={context} />);
}

function Block({ block, context }: { block: MarkdownBlock; context: RenderContext }) {
  switch (block.kind) {
    case "heading":
      return <Heading level={block.level} text={block.text} context={context} />;
    case "paragraph":
      return (
        <p>
          <Lines lines={block.lines} context={context} />
        </p>
      );
    case "list":
      return <List block={block} context={context} />;
    case "code":
      return (
        // 長いコードは枠の中だけで横にスクロールする。スクロールする領域はキーボードでも動かせるようにフォーカスを受ける。
        <pre
          tabIndex={0}
          className="max-w-full overflow-x-auto rounded-md border border-border bg-surface-sunken p-3 font-mono text-xs leading-5 text-fg [overflow-wrap:normal]"
          data-language={block.language || undefined}
        >
          <code>{block.text}</code>
        </pre>
      );
    case "quote":
      return (
        <blockquote className="space-y-3 border-l-2 border-border-strong pl-3 text-fg-muted">
          {renderBlocks(block.blocks, context)}
        </blockquote>
      );
    case "rule":
      return <hr className="border-border" />;
    default:
      return null;
  }
}

function Heading({ level, text, context }: { level: number; text: string; context: RenderContext }) {
  const rank = Math.max(0, level - context.topLevel);
  const htmlLevel = Math.min(6, context.headingLevel + rank);
  const Tag = `h${htmlLevel}` as "h2" | "h3" | "h4" | "h5" | "h6";
  // 最上位の見出しはセクション見出しの大きさ（16px / 600）、その下は本文の大きさの太字。
  return (
    <Tag className={cn("font-semibold text-fg", rank === 0 ? "text-base" : "text-sm")}>
      <Inline parts={parseMarkdownInline(text)} context={context} />
    </Tag>
  );
}

function List({ block, context }: { block: Extract<MarkdownBlock, { kind: "list" }>; context: RenderContext }) {
  const items = block.items.map((item, index) => (
    <li key={index} className="space-y-1 pl-1 marker:text-fg-muted">
      {item.map((child, childIndex) =>
        // 項目の段落は余白の無い文として出す（箇条書きの行の高さをそろえる）。
        child.kind === "paragraph" ? (
          <p key={childIndex}>
            <Lines lines={child.lines} context={context} />
          </p>
        ) : (
          <Block key={childIndex} block={child} context={context} />
        ),
      )}
    </li>
  ));
  if (block.ordered) {
    return (
      <ol start={block.start === 1 ? undefined : block.start} className="list-decimal space-y-1 pl-6">
        {items}
      </ol>
    );
  }
  return <ul className="list-disc space-y-1 pl-5">{items}</ul>;
}

function Lines({ lines, context }: { lines: string[]; context: RenderContext }) {
  return (
    <>
      {lines.map((line, index) => (
        <Fragment key={index}>
          {index > 0 ? <br /> : null}
          <Inline parts={parseMarkdownInline(line)} context={context} />
        </Fragment>
      ))}
    </>
  );
}

function Inline({ parts, context }: { parts: MarkdownInline[]; context: RenderContext }) {
  return (
    <>
      {parts.map((part, index) => {
        switch (part.kind) {
          case "code":
            return (
              <code key={index} className="rounded-sm bg-surface-sunken px-1 py-0.5 font-mono text-xs text-fg">
                {part.text}
              </code>
            );
          case "strong":
            return (
              <strong key={index} className="font-semibold">
                <Inline parts={part.children} context={context} />
              </strong>
            );
          case "em":
            return (
              <em key={index}>
                <Inline parts={part.children} context={context} />
              </em>
            );
          case "link": {
            const external = /^https?:/i.test(part.href);
            return (
              <a
                key={index}
                href={part.href}
                target={external ? "_blank" : undefined}
                rel={external ? "noopener noreferrer" : undefined}
                className="text-accent-fg underline underline-offset-2 hover:text-accent-fg-strong"
              >
                <Inline parts={part.children} context={context} />
                {/* 読み上げの補足は部品の文言なので、本文の言語ではなく画面の言語で読む（#3349）。 */}
                {external ? (
                  <span className="sr-only" lang={uiTextLang()}>
                    {context.labels.newTab}
                  </span>
                ) : null}
              </a>
            );
          }
          default:
            return <Fragment key={index}>{part.text}</Fragment>;
        }
      })}
    </>
  );
}
