import {
  AnswerEvidence,
  AnswerSections,
  EvidenceSourceSheet,
  MarkdownInlineText,
  useEvidenceSource,
  type AnswerCitationRef,
  type AnswerPassageRecords,
  type AnswerSectionsBlock,
  type EvidenceSourceState,
} from "@production-ready/ui";

import { AnswerBody } from "@/components/chat/ResultTables";
import { RagSourcePreview } from "@/components/evidence/RagSourcePreview";
import {
  EVIDENCE_LABELS,
  citationLabel,
  type AnswerEvidenceData,
} from "@/lib/answer-evidence";
import { answerPassageLabels, type StructuredBlock } from "@/lib/answer-structure";

/**
 * 回答の根拠の塊（#2328）。RAG のチャットと同じ共有の `AnswerEvidence`（「AI に渡した N 件 ／ 回答で引用 M 件」の
 * 折りたたみ・カード・一定の高さの中のスクロール）。回答の本文の下・回答の評価の前に置く。
 * カードの「根拠を表示」（共通の部品が描く。#2388）は、本文の根拠の行と同じ右のサイドパネル（共通の
 * `EvidenceSourceSheet`。#2360）を開く。
 */
export function AnswerEvidenceBlock({
  runId,
  evidence,
  withheldCount = 0,
  idPrefix,
  className,
}: {
  runId: string;
  /** 見られる根拠が 1 件も無い（すべて権限の無い根拠）ときは null。 */
  evidence: AnswerEvidenceData | null;
  /**
   * 保存した根拠のうち、見ている利用者が今の権限で見られない根拠の数（backend の `withheld_evidence_count`。中身は
   * 届かない。RAG のチャットと同じ共通の行。#3252）。
   */
  withheldCount?: number;
  idPrefix: string;
  className?: string;
}) {
  const source = useEvidenceSource<number>();
  return (
    <>
      <AnswerEvidence
        items={evidence?.entries.map((entry) => entry.item) ?? []}
        withheldCount={withheldCount}
        labels={EVIDENCE_LABELS}
        idPrefix={idPrefix}
        className={className}
        onOpenSource={source.open}
      />
      {evidence ? (
        <EvidenceSource
          runId={runId}
          evidence={evidence}
          source={source}
          testId={`${idPrefix}-source-sheet`}
        />
      ) : null}
    </>
  );
}

/**
 * 根拠 1 件の右のサイドパネル（RAG のチャットと同じ共通の `EvidenceSourceSheet`。#2360）。上部に根拠の要約、その下に
 * RAG と同じ共通の原文のプレビュー（ページの画像・強調。#2389）を出す。原文は RAG の画面用の Cookie の API ではなく、
 * Agent の backend が閲覧者として RAG に作らせる短命の署名つきの URL で読む。
 */
function EvidenceSource({
  runId,
  evidence,
  source,
  testId,
}: {
  runId: string;
  evidence: AnswerEvidenceData;
  source: EvidenceSourceState<number>;
  testId: string;
}) {
  if (!source.mounted || source.shown == null) return null;
  const entry = evidence.entries[source.shown];
  const open = source.selected !== null;
  return (
    <EvidenceSourceSheet
      open={open}
      onClose={source.close}
      item={entry?.item ?? null}
      index={source.shown}
      labels={EVIDENCE_LABELS}
      data-testid={testId}
    >
      {entry && !entry.attachment ? (
        <RagSourcePreview
          // 根拠を切り替えたら、倍率・回転・ページを初期化する。
          key={`${entry.documentId}:${entry.chunkId}`}
          runId={runId}
          documentId={entry.documentId}
          chunkId={entry.chunkId}
          fileName={entry.item.fileName}
          quote={entry.item.text}
          open={open}
        />
      ) : null}
    </EvidenceSourceSheet>
  );
}

/**
 * 回答の本文の節（RAG のチャットの回答と同じ共有の `AnswerSections`。#2328）。段落・項目の下の「根拠：文書 p.N」を
 * 押すと、その根拠を右のサイドパネルで開く（RAG の根拠の行と同じ共通の `EvidenceSourceSheet`。#2360）。
 * 段落の印の記録（`passages`。#2738）があれば、印の付いた段落に線と印を付け、回答全体の注意・外した段落・
 * 確認が必要な点を共有の部品で出す（RAG のチャットと同じ見た目）。根拠の無い回答（`evidence` が null）も描ける。
 */
export function StructuredAnswer({
  runId,
  blocks,
  evidence,
  passages = null,
  markdownHeadingLevel = 2,
  testId,
}: {
  runId: string;
  blocks: StructuredBlock[];
  evidence: AnswerEvidenceData | null;
  passages?: AnswerPassageRecords | null;
  /** Markdown の塊の最上位の見出しの段（置く場所の見出しの下。チャットは見出し「回答」h3 の下の 4。#3433）。 */
  markdownHeadingLevel?: 2 | 3 | 4;
  testId: string;
}) {
  const source = useEvidenceSource<number>();
  const byChunk = new Map((evidence?.entries ?? []).map((entry, index) => [entry.chunkId, { entry, index }]));
  const citations = (chunkIds: readonly string[]): AnswerCitationRef[] => {
    const refs: AnswerCitationRef[] = [];
    for (const chunkId of chunkIds) {
      const found = byChunk.get(chunkId);
      if (!found) continue;
      const label = citationLabel(found.entry);
      // 同じ文書・頁の根拠は 1 行にする。
      if (refs.some((ref) => ref.label === label)) continue;
      refs.push({
        key: chunkId,
        label,
        location: found.entry.citationLocation || undefined,
        ariaLabel: EVIDENCE_LABELS.citationOpen(label),
        onOpen: () => source.open(found.index),
      });
    }
    return refs;
  };
  const sections: AnswerSectionsBlock[] = blocks.map((block) => {
    if (block.kind === "paragraph") {
      return {
        kind: "paragraph",
        key: block.key,
        content: <MarkdownInlineText text={block.text} />,
        citations: citations(block.chunkIds),
        passageText: block.raw ?? block.text,
      };
    }
    if (block.kind === "markdown") {
      return {
        kind: "content",
        key: block.key,
        content: <AnswerBody text={block.text} headingLevel={markdownHeadingLevel} />,
        citations: citations(block.chunkIds),
        passageText: block.raw ?? block.text,
      };
    }
    return {
      kind: "section",
      key: block.key,
      title: block.title ?? undefined,
      ordered: block.ordered,
      collapsible: block.collapsible,
      entries: block.entries.map((entry) => ({
        key: entry.key,
        // 根拠の原文・決まった説明の文は Markdown として読まない（原文の記号を強調にしない）。
        content: entry.plain ? entry.text : <MarkdownInlineText text={entry.text} />,
        muted: entry.muted,
        // 画面の決まった説明の文は回答の言語の lang を付けない（#3349）。
        lang: entry.uiText ? null : undefined,
        citations: citations(entry.chunkIds),
        passageText: entry.raw ?? entry.text,
      })),
    };
  });
  return (
    <AnswerSections blocks={sections} testId={testId} passages={passages} passageLabels={answerPassageLabels()}>
      {evidence ? (
        <EvidenceSource runId={runId} evidence={evidence} source={source} testId={`${testId}-citation-sheet`} />
      ) : null}
    </AnswerSections>
  );
}
