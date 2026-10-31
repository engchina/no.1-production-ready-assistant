import {
  AnswerEvidence,
  EvidenceList,
  EvidenceWithheldNote,
  useEvidenceSource,
  type EvidenceSourceState,
} from "@production-ready/ui";

import type { RetrievedChunk } from "@/lib/api";

import { CitationActions, type CitationCardOptions } from "./CitationCard";
import { citationEvidenceItem, EVIDENCE_LABELS } from "./citation-evidence";
import { CitationSourceSheet } from "./CitationSourceSheet";

/**
 * 回答の根拠（チャット・RAG 検索・回答の記録・実行の詳細で共通。#2097）。「AI に渡した N 件 ／ 回答で引用 M 件 …」の
 * 折りたたみ（既定は閉じる）に、AI に渡した箇所の一覧を入れる。回答の本文の直後、回答の評価の前に置く。
 * 折りたたみ・見出し・カード・「根拠を表示」は Agent と同じ共通の `AnswerEvidence`（#2328・#2388。まとまりの
 * 表示・見方の切り替えは持たない。文書名と節はカードのチップにある）。
 */
export function AnswerCitations({
  citations,
  idPrefix,
  cardProps,
  className,
  listTestId,
  withheldCount = 0,
}: {
  citations: RetrievedChunk[];
  idPrefix: string;
  cardProps?: CitationCardOptions;
  className?: string;
  listTestId?: string;
  /**
   * 保存した根拠のうち、見ている利用者が今の権限で見られない根拠の数（backend の `withheld_evidence_count`。中身は
   * 届かない。#3252）。
   */
  withheldCount?: number;
}) {
  const source = useEvidenceSource<number>();
  if (!citations.length && withheldCount <= 0) return null;
  const preview = cardProps?.preview ?? true;
  return (
    <>
      <AnswerEvidence
        items={citations.map(citationEvidenceItem)}
        idPrefix={idPrefix}
        labels={EVIDENCE_LABELS}
        className={className}
        listTestId={listTestId}
        withheldCount={withheldCount}
        onOpenSource={preview ? source.open : undefined}
        renderActions={(_item, index) => <CitationActions chunk={citations[index]} {...cardProps} />}
      />
      {preview ? <Preview citations={citations} source={source} kind="answer" /> : null}
    </>
  );
}

/**
 * 回答を作らない検索の結果の一覧（RAG 検索・実行の詳細。#2097）。カードと「根拠を表示」は回答の根拠と同じ共通の
 * `EvidenceList`（#2388）で、見出しは画面が「検索結果（N）」を出す。
 */
export function CitationList({
  citations,
  idPrefix,
  cardProps = {},
  listTestId,
  withheldCount = 0,
}: {
  citations: RetrievedChunk[];
  /** 同じ画面に複数の一覧があるときに、開くボタンの id を分ける。 */
  idPrefix: string;
  cardProps?: CitationCardOptions;
  listTestId?: string;
  /** 保存した結果のうち、見ている利用者が今の権限で見られない根拠の数（#3252）。 */
  withheldCount?: number;
}) {
  const source = useEvidenceSource<number>();
  const preview = cardProps.preview ?? true;
  return (
    <>
      <EvidenceWithheldNote count={withheldCount} labels={EVIDENCE_LABELS} className="mb-2" />
      <EvidenceList
        items={citations.map(citationEvidenceItem)}
        idPrefix={idPrefix}
        labels={EVIDENCE_LABELS}
        testId={listTestId}
        onOpenSource={preview ? source.open : undefined}
        renderActions={(_item, index) => <CitationActions chunk={citations[index]} {...cardProps} />}
      />
      {preview ? <Preview citations={citations} source={source} kind="search" /> : null}
    </>
  );
}

/**
 * 根拠の右のサイドパネル（#2360）。カードの「根拠を表示」から開く（1 つの一覧に 1 つ）。一度開くまでは描かず、
 * 閉じた後は描いたままにして開いた操作へフォーカスを戻す。
 */
function Preview({
  citations,
  source,
  kind,
}: {
  citations: RetrievedChunk[];
  source: EvidenceSourceState<number>;
  kind: "answer" | "search";
}) {
  if (!source.mounted || source.shown == null) return null;
  return (
    <CitationSourceSheet
      chunk={citations[source.shown] ?? null}
      index={source.shown}
      open={source.selected !== null}
      onClose={source.close}
      kind={kind}
    />
  );
}
