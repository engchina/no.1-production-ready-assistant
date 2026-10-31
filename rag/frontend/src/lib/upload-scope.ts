import type { CurrentUser } from "./api";

/**
 * アップロード先のナレッジベース（#214）。
 * 利用できる KB が制限された利用者（`allowed_knowledge_base_ids` が null でない）は、KB を指定しない
 * アップロードが backend で 400 になる（DEFAULT へ黙って登録しない）。画面は送信前に KB の選択を求める。
 */
export function uploadKnowledgeBaseRequired(
  user: Pick<CurrentUser, "allowed_knowledge_base_ids"> | null | undefined,
): boolean {
  return Boolean(user && user.allowed_knowledge_base_ids !== null);
}

/** 送信してよいか。KB の選択が必須で 1 件も選ばれていなければ false。 */
export function canSubmitUpload(required: boolean, selectedKnowledgeBaseIds: readonly string[]): boolean {
  return !required || selectedKnowledgeBaseIds.length > 0;
}
