import { useEffect, useRef, useState, type Dispatch, type SetStateAction } from "react";

/**
 * ページ遷移・再読込のときに残す作業状態（platform UX 契約 workspace-state.md。#87）。
 *
 * - 保存先は同じタブの `sessionStorage` だけ。ブラウザを閉じれば消え、恒久保存の代わりにしない。
 * - 保存してよい field は `WORKSPACE_FIELDS` の allowlist に明示登録したものだけ。
 *   秘密情報・資格情報・確認語・サーバー応答全体は登録しない。
 * - 各レコードは期限（`WORKSPACE_TTL_MS`）と文字数の上限を持つ。読めない・壊れた・期限切れの値は捨てて既定値に戻す。
 * - storage が使えない環境（プライベートモード等）でも画面は既定値で動く（読み書きはすべて try/catch）。
 */
export const WORKSPACE_NAMESPACE = "production-ready-agent.workspace.v1:";
export const WORKSPACE_TTL_MS = 8 * 60 * 60 * 1000;
const MAX_VALUE_CHARS = 20_000;

/**
 * 保存してよい field の allowlist（ページ → field）。
 * A 型（一覧 → 全画面エディタ）の編集対象は URL の `?id=` が唯一の情報源なので、ここには置かない（#137）。
 */
export const WORKSPACE_FIELDS = {
  // チャット（#768）: 選んだ Agent・会話と、送る前の下書き。会話の履歴のページ（Agent と offset。#1265）。
  chat: ["agentId", "threadId", "draft", "historyOpen", "historyPage"],
  // 業務 Agent の編集画面の「試す」（#2515）: チャットと同じ field を、下書きの会話の分として別に持つ。
  agentTry: ["agentId", "threadId", "draft", "historyOpen", "historyPage"],
  runs: ["selectedRunId", "goal"],
  // 承認の一覧で選んだ承認（#808）。判断は確認し直すので残さない。
  approvals: ["selectedId"],
  audit: ["filterForm", "appliedForm", "page"],
  // フィードバックの絞り込み（#774）と、見方のタブ・確かめるべき実行のシグナルの種類（#1822）。
  feedback: ["days", "agentId", "rating", "reason", "view", "signalKind"],
  // 品質評価の業務 Agent・表示している評価（#776）。評価ケースは評価セットとして保存する。
  // 評価を比べる 2 つの評価（#1845）。
  evaluation: ["agentId", "jobId", "compareBase", "compareTarget"],
  // 利用状況の期間と内訳のタブ（#772）。
  usage: ["days", "view"],
  // 一覧のページ番号（#265）。一覧 → エディタ → 一覧の移動と再読込で同じページに戻す。
  lists: ["skillUpdates", "agents", "runs", "approvals", "tools", "mcpServers", "skills", "evaluationSets", "evaluationResults", "evaluationJobs", "evaluationCompare", "feedback", "feedbackSignals", "usage"],
  // 一覧の表示件数（10 / 25 / 50 / 100。#1891）。ページ番号と同じ一覧の名前に、監査ログを足す。
  listPageSize: ["skillUpdates", "agents", "runs", "approvals", "tools", "mcpServers", "skills", "evaluationSets", "evaluationResults", "evaluationJobs", "evaluationCompare", "feedback", "feedbackSignals", "usage", "audit"],
  // 一覧の絞り込みの検索語（SearchField が確定した値。#535 / #808）。
  listSearch: ["agents", "runs", "approvals", "tools", "mcpServers", "skills", "toolPolicy"],
  // 一覧の状態の絞り込み（実行履歴の状態・承認の保留中 / 判断済み。#808）と、実行履歴の実行者の絞り込み
  // （すべて / 自分が実行 / 他の人が実行。#1954）と、種類（きっかけ）・期間（#1955）。
  listFilter: ["runs", "runsKind", "runsPeriod", "runsExecutor", "approvals"],
} as const;

export type WorkspacePage = keyof typeof WORKSPACE_FIELDS;
export type WorkspaceField<P extends WorkspacePage> = (typeof WORKSPACE_FIELDS)[P][number];
export type WorkspaceValidator<T> = (value: unknown) => value is T;

interface StoredRecord {
  value: unknown;
  expiresAt: number;
}

function storageKey(page: WorkspacePage, field: string): string {
  return `${WORKSPACE_NAMESPACE}${page}.${field}`;
}

function isAllowed(page: WorkspacePage, field: string): boolean {
  return (WORKSPACE_FIELDS[page] as readonly string[]).includes(field);
}

function sameTypeAs<T>(initial: T): WorkspaceValidator<T> {
  return (value: unknown): value is T => typeof value === typeof initial && value !== null;
}

export const isString: WorkspaceValidator<string> = (value): value is string => typeof value === "string";

export const isNullableString: WorkspaceValidator<string | null> = (value): value is string | null =>
  value === null || typeof value === "string";

export function isOneOf<T extends string>(values: readonly T[]): WorkspaceValidator<T> {
  return (value: unknown): value is T => typeof value === "string" && (values as readonly string[]).includes(value);
}

/** 保存済みの値を読む。無い・壊れた・期限切れ・型違いのときは `initial` を返す。 */
export function readWorkspaceValue<P extends WorkspacePage, T>(
  page: P,
  field: WorkspaceField<P>,
  initial: T,
  isValid: WorkspaceValidator<T> = sameTypeAs(initial)
): T {
  if (!isAllowed(page, field)) return initial;
  try {
    const raw = window.sessionStorage.getItem(storageKey(page, field));
    if (raw === null) return initial;
    const record = JSON.parse(raw) as Partial<StoredRecord> | null;
    if (!record || typeof record.expiresAt !== "number" || record.expiresAt <= Date.now()) {
      window.sessionStorage.removeItem(storageKey(page, field));
      return initial;
    }
    return isValid(record.value) ? record.value : initial;
  } catch {
    return initial;
  }
}

/** 値を保存する。既定値と同じなら消す。保存できなかったら false を返す。 */
export function writeWorkspaceValue<P extends WorkspacePage>(
  page: P,
  field: WorkspaceField<P>,
  value: unknown,
  initial?: unknown
): boolean {
  if (!isAllowed(page, field)) return false;
  try {
    const key = storageKey(page, field);
    if (initial !== undefined && JSON.stringify(value) === JSON.stringify(initial)) {
      window.sessionStorage.removeItem(key);
      return true;
    }
    const raw = JSON.stringify({ value, expiresAt: Date.now() + WORKSPACE_TTL_MS } satisfies StoredRecord);
    if (raw.length > MAX_VALUE_CHARS) return false;
    window.sessionStorage.setItem(key, raw);
    return true;
  } catch {
    return false;
  }
}

/** 作業状態の持ち主（ログイン中の利用者の user_uuid）を記録する key。 */
const OWNER_KEY = `${WORKSPACE_NAMESPACE}owner`;

/** この製品の作業状態をすべて消す（ログアウト・認証の失効・アカウント切替で呼ぶ）。 */
export function clearWorkspaceState(): void {
  try {
    const keys: string[] = [];
    for (let index = 0; index < window.sessionStorage.length; index += 1) {
      const key = window.sessionStorage.key(index);
      if (key?.startsWith(WORKSPACE_NAMESPACE)) keys.push(key);
    }
    keys.forEach((key) => window.sessionStorage.removeItem(key));
  } catch {
    // storage が使えない環境では消すものもない。
  }
}

/**
 * 作業状態を今の利用者に結び付ける（#215）。前回の持ち主と違えば、別の利用者の下書き・選択を
 * 使い回さないよう先に消す（workspace-state.md「logout・認証の失効・アカウントの変更で消す」）。
 */
export function bindWorkspaceOwner(owner: string): void {
  try {
    if (window.sessionStorage.getItem(OWNER_KEY) === owner) return;
    clearWorkspaceState();
    window.sessionStorage.setItem(OWNER_KEY, owner);
  } catch {
    // storage が使えない環境では保存もしないため、結び付けるものもない。
  }
}

/**
 * `useState` と同じ形で、allowlist の field を sessionStorage に残す。
 * 3 番目の戻り値は直近の保存に成功したか（失敗時は呼び出し側で離脱の保護を出す）。
 */
export function useWorkspaceState<P extends WorkspacePage, T>(
  page: P,
  field: WorkspaceField<P>,
  initial: T,
  isValid?: WorkspaceValidator<T>
): [T, Dispatch<SetStateAction<T>>, boolean] {
  const initialRef = useRef(initial);
  const [value, setValue] = useState<T>(() => readWorkspaceValue(page, field, initial, isValid));
  const [saved, setSaved] = useState(true);

  useEffect(() => {
    setSaved(writeWorkspaceValue(page, field, value, initialRef.current));
  }, [page, field, value]);

  return [value, setValue, saved];
}
