/**
 * 回答前の確認（#1839）の文言。3 製品で同じ書き方にし、製品の名詞だけを変える（AGENTS.md「言語」）。
 * 製品は `clarificationLabels({ stage, action })` で名詞だけを渡し、i18n で個別に上書きするときも同じ文型を保つ。
 */

/** 製品ごとに変える名詞（例: RAG・Agent は `{ stage: "回答", action: "回答" }`、NL2SQL は `{ stage: "SQL の生成", action: "SQL を生成" }`）。 */
export interface ClarificationNouns {
  /** 確認の後に行うこと（題に入る名詞）。「〈stage〉の前に確認させてください」。 */
  stage: string;
  /** 確定・スキップのボタンの動詞句（「する」を除く）。「この内容で〈action〉」「確認せずに〈action〉」。 */
  action: string;
}

/** 確認事項の答え方（選択肢から選ぶ・自由入力だけで答える）と、複数の確認事項でそれが混ざること。 */
export type ClarificationAnswerKind = "choice" | "freeText" | "mixed";

export interface ClarificationLabels {
  /** カードの題（例:「回答の前に確認させてください」）。 */
  title: string;
  /** 1 件ずつ聞くときの位置（例:「確認事項 1 / 3」）。 */
  step: (current: number, total: number) => string;
  /** 1 枚のカードで複数の確認事項を聞くときの件数（例:「確認事項 2 件」）。 */
  itemCount: (count: number) => string;
  /** 確認の往復の何回目か（例:「確認 1 回目（上限 2 回）」）。 */
  round: (current: number, max: number) => string;
  /** 往復の上限に達した（最後の確認）ときの補足。 */
  lastRound: string;
  /** 確認する理由の見出し（読み上げにも使う）。 */
  reason: string;
  /** 複数選択の確認事項の補足。 */
  multipleHint: string;
  /** 選択肢のある確認事項の自由入力の欄のラベル（選択肢に無い答えの逃げ道）。 */
  other: string;
  otherPlaceholder: string;
  /** 選択肢の無い（自由入力だけで答える）確認事項の欄のラベル。「その他」と言わない（#3366）。 */
  freeText: string;
  freeTextPlaceholder: string;
  /** 確定のボタン（例:「この内容で回答」）。 */
  confirm: string;
  /** 確定を押せない理由（1 件。選択肢のある確認事項）。 */
  confirmBlocked: string;
  /** 確定を押せない理由（1 件。選択肢の無い確認事項）。 */
  confirmBlockedFreeText: string;
  /**
   * 確定を押せない理由（複数の確認事項。`remaining` は答えていない件数、`kind` は答えていない確認事項の答え方:
   * すべて選択肢あり `choice` / すべて自由入力だけ `freeText` / 混在 `mixed`）。
   */
  confirmBlockedMultiple: (remaining: number, kind?: ClarificationAnswerKind) => string;
  /** スキップのボタン（例:「確認せずに回答」）。 */
  skip: string;
  /** スキップの補足（例:「前提を示して回答します。」）。空なら出さない。 */
  skipHint: string;
  /** 確定の後、フォームを置き換える経過の行の文（例:「答えを反映しています」）。 */
  confirming: string;
  /** 会話に残す表示の題。 */
  recordTitle: string;
  /** 会話に残す表示の自由入力の答え。 */
  recordOther: (text: string) => string;
  /** 会話に残す表示で、答えの無い確認事項。 */
  recordNoAnswer: string;
  /** 会話に残す表示で、スキップしたとき（例:「確認せずに回答しました（前提を示して回答）」）。 */
  recordSkipped: string;
  /** 会話に残す表示で、選んだ選択肢をつなぐ文字。 */
  recordSeparator: string;
}

/** 英数字で始まる名詞の前に空白を入れる（「この内容で SQL を生成」。日本語の地の文の書き方）。 */
function spaced(prefix: string, noun: string): string {
  return /^[A-Za-z0-9]/.test(noun) ? `${prefix} ${noun}` : `${prefix}${noun}`;
}

/** 製品の名詞から文言を組む。 */
export function clarificationLabels(nouns: ClarificationNouns): ClarificationLabels {
  const { stage, action } = nouns;
  return {
    title: `${stage}の前に確認させてください`,
    step: (current, total) => `確認事項 ${current} / ${total}`,
    itemCount: (count) => `確認事項 ${count} 件`,
    round: (current, max) => `確認 ${current} 回目（上限 ${max} 回）`,
    lastRound: `これが最後の確認です。決まらない点は${spaced("前提を示して", action)}します。`,
    reason: "確認する理由",
    multipleHint: "当てはまるものをすべて選べます。",
    other: "その他（自由入力）",
    otherPlaceholder: "選択肢に無いときは、ここに書いてください",
    freeText: "答え",
    freeTextPlaceholder: "答えを書いてください",
    confirm: spaced("この内容で", action),
    confirmBlocked: "選択肢を選ぶか、「その他（自由入力）」に書くと押せます。",
    confirmBlockedFreeText: "答えを書くと押せます。",
    confirmBlockedMultiple: (remaining, kind = "choice") =>
      kind === "freeText"
        ? `すべての確認事項で答えを書くと押せます（残り ${remaining} 件）。`
        : kind === "mixed"
          ? `すべての確認事項に答えると押せます（残り ${remaining} 件）。`
          : `すべての確認事項で、選択肢を選ぶか「その他（自由入力）」に書くと押せます（残り ${remaining} 件）。`,
    skip: spaced("確認せずに", action),
    skipHint: `${spaced("前提を示して", action)}します。`,
    confirming: "答えを反映しています",
    recordTitle: `${stage}前に確認した内容`,
    recordOther: (text) => `その他: ${text}`,
    recordNoAnswer: "（答えなし）",
    recordSkipped: `${spaced("確認せずに", action)}しました（${spaced("前提を示して", action)}）`,
    recordSeparator: "、",
  };
}

/** 既定の文言（RAG・Agent の「回答」）。 */
export const DEFAULT_CLARIFICATION_LABELS: ClarificationLabels = clarificationLabels({
  stage: "回答",
  action: "回答",
});
