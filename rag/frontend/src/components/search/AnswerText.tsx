import {
  AnswerPlainText,
  AnswerSections,
  useEvidenceSource,
  type AnswerCitationRef,
  type AnswerSectionEntry,
  type AnswerSectionsBlock,
} from "@production-ready/ui";
import { ExternalLink } from "lucide-react";
import { Link } from "react-router-dom";
import type { ReactNode } from "react";

import type { RetrievedChunk } from "@/lib/api";
import { answerPassageLabels, answerPassages, withoutPassageRecordSections } from "@/lib/answer-passages";
import {
  answerFormat,
  citationLocation,
  parseAnswerText,
  parseCitationLine,
  resolveCitationLineTargets,
  type AnswerTextBlock,
  type AnswerTextEntry,
  type CitationTarget,
} from "@/lib/answer-text";
import type { UnreadSectionLink } from "@/lib/answer-diagnostics";
import { t } from "@/lib/i18n";
import { APP_ROUTES } from "@/lib/routes";

import { CitationSourceSheet } from "./CitationSourceSheet";

// rag_engine の grounded.py の UNVERIFIED_SECTION_TITLE（#1436・#2040）。
const UNREAD_SECTION_TITLE = "回答に未反映の関連章節";
// rag_engine の grounded.py の GAPS_SECTION_TITLE。項目が多ければ共通の「すべて表示」で畳む（Agent の同じ節と
// 同じ規則。#2361）。
const GAPS_SECTION_TITLE = "資料から確認できなかった事項";

/**
 * 回答本文（#651）。回答エンジンの書式（節の見出し・説明・「根拠：」の行）に合う本文は、節・説明・根拠に分けて出す。
 * 生成中と、書式に合わない本文（根拠不足の案内など）はそのままの文字列で出す。
 * 根拠の行は、当たる引用（`citations`）があれば押せるようにし、その引用の原文のプレビューを開く（#657）。
 * 当たる引用は、回答エンジンが付けた出典行の番号で決める（無ければファイル名と頁。#1330）。位置が確定できない行は
 * 候補の引用を開き、位置は未確定と示して原文を強調しない（#2710）。
 * 回答の記録の診断（`diagnostics`）に段落の印の記録（#2738）があれば、印の付いた段落・項目に線と印を付け、本文の上に
 * 回答全体の注意、本文の後ろに「根拠と矛盾し除いた内容」「確認が必要な点」を出す（Agent と同じ共有の部品）。書式に
 * 合わない本文も、記録があれば空行で段落に分けて印を付ける。記録の無い過去の回答は今までの表示のまま。
 */
export function AnswerText({
  text,
  streaming = false,
  cursor = null,
  live = false,
  citations = [],
  unreadSections = [],
  diagnostics,
}: {
  text: string;
  streaming?: boolean;
  /** 生成中に本文の後ろに出すカーソル。 */
  cursor?: ReactNode;
  /** 本文の更新を読み上げる（チャット）。 */
  live?: boolean;
  /** この回答の引用。根拠の行から原文のプレビューを開くのに使う。 */
  citations?: readonly RetrievedChunk[];
  /**
   * 「回答に未反映の関連章節」の行ごとの章節（#2040）。当たる行に章節の原文を開くリンクを付ける。
   * 文書の詳細を開けない利用者には渡さない（呼び出し側が `canOpenDocumentDetail` で決める）。
   */
  unreadSections?: readonly UnreadSectionLink[];
  /** 回答の記録の診断（`diagnostics.answer`）。段落の印の記録（#2738）を読む。生成中は渡さない。 */
  diagnostics?: unknown;
}) {
  const source = useEvidenceSource<CitationTarget>();
  const format = answerFormat(diagnostics);
  const passages = streaming ? null : answerPassages(diagnostics);
  // 本文の末尾の「根拠と矛盾し除いた内容」「確認が必要な点」は記録から共有の部品が作るので、本文の節は描かない（#3488）。
  const body = passages ? withoutPassageRecordSections(text, format) : text;
  const blocks = streaming ? null : (parseAnswerText(body, format) ?? (passages ? plainParagraphs(body) : null));
  // 回答エンジンの決まった文言（保存した表示形式の値と同じ段落・説明）は画面の言語で描く（回答の言語の lang を付けない。#3349）。
  const fixedTexts = new Set(Object.values(format ?? {}));
  const fixedLang = (value: string) => (fixedTexts.has(value) ? null : undefined);
  if (!blocks) {
    return (
      // 節に分けない本文も共通の部品で出し、回答の言語の字形で描く（#3335）。
      <AnswerPlainText text={text} live={live}>
        {cursor}
      </AnswerPlainText>
    );
  }
  // 節・段落・根拠の行の見た目は 3 製品で共通の AnswerSections（#2328）。
  return (
    <AnswerSections
      live={live}
      passages={passages}
      passageLabels={answerPassageLabels()}
      blocks={blocks.map((block, index): AnswerSectionsBlock =>
        block.kind === "paragraph"
          ? { kind: "paragraph", key: String(index), content: block.text, lang: fixedLang(block.text) }
          : {
              kind: "section",
              key: String(index),
              title: block.title,
              ordered: block.ordered,
              collapsible: block.title === (format?.gaps_section ?? GAPS_SECTION_TITLE),
              entries: block.entries.map((entry, entryIndex) => toEntry(entry, entryIndex, block.title)),
            }
      )}
    >
      {/* 根拠の行から、根拠の一覧の「根拠を表示」と同じ右のサイドパネルを開く（#2360）。 */}
      {source.mounted && source.shown != null ? (
        <CitationSourceSheet
          chunk={citations[source.shown.index] ?? null}
          index={source.shown.index}
          position={source.shown.position}
          open={source.selected !== null}
          onClose={source.close}
        />
      ) : null}
    </AnswerSections>
  );

  function toCitations(citation: string, ordinal: number | undefined): AnswerCitationRef[] {
    const ref = parseCitationLine(citation, format);
    const targets = resolveCitationLineTargets(ordinal, ref, citations);
    if (!targets.length) return [{ key: `${ordinal}:${citation}`, label: citation }];
    return targets.map((target, index) => {
      const label = targets.length === 1 ? citation : t(
        target.position.kind === "unresolved"
          ? "search.answerText.citationCandidates"
          : "search.answerText.citationPart",
        { citation, number: index + 1, total: targets.filter((entry) => entry.position.kind === "exact").length },
      );
      return {
        key: `${ordinal}:${citations[target.index].document_id}:${citations[target.index].chunk_id ?? target.index}:${target.position.kind}`,
        label,
        location: citationLocation(citation, ref?.fileName),
        ariaLabel: t("search.answerText.openCitation", { citation: label }),
        onOpen: () => source.open(target),
      };
    });
  }

  function renderSectionLink(text: string, title: string) {
    if (title !== (format?.unverified_section ?? UNREAD_SECTION_TITLE)) return null;
    const section = unreadSections.find((item) => item.line === text);
    if (!section) return null;
    const query = section.page != null ? `?page=${section.page}` : "";
    return (
      <span className="mt-0.5 flex">
        <Link
          to={`${APP_ROUTES.documents}/${encodeURIComponent(section.documentId)}${query}`}
          target="_blank"
          rel="noopener noreferrer"
          aria-label={t("search.answerText.openUnreadSectionLabel", { section: section.label })}
          className="inline-flex items-center gap-1 text-xs font-medium text-accent-fg hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring"
        >
          {t("search.answerText.openUnreadSection")}
          <ExternalLink size={14} aria-hidden />
        </Link>
      </span>
    );
  }

  function toEntry(entry: AnswerTextEntry, index: number, title: string): AnswerSectionEntry {
    if (entry.kind === "text") {
      return { key: String(index), content: entry.text, muted: true, lang: fixedLang(entry.text) };
    }
    return {
      key: String(index),
      content: entry.text,
      citations: entry.citations.flatMap((citation, position) => toCitations(citation, entry.citationLines[position])),
      extra: renderSectionLink(entry.text, title),
    };
  }
}

/** 書式の見出しの無い本文を、空行で段落に分ける（段落の印の記録のある回答だけ。#2738）。 */
function plainParagraphs(text: string): AnswerTextBlock[] {
  return text
    .split(/\n\s*\n/)
    .map((part) => part.trim())
    .filter(Boolean)
    .map((part) => ({ kind: "paragraph", text: part }));
}
