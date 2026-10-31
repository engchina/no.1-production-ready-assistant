import { deletedUserLabel } from "@production-ready/system-settings";
import { t } from "@/lib/i18n";

/** 利用者の表示名の対応表（ログイン ID または UUID → 表示名）。一覧の応答の `user_display_names`。 */
export type UserDisplayNames = Readonly<Record<string, string>>;

/**
 * ログイン ID だけを記録した項目（承認の判断者・版の公開者）を画面に出す名前（#1696）。
 *
 * 画面の利用者は表示名で出し、ログイン ID・UUID は出さない（サイドナビのアカウント欄の 1 行目だけがログイン ID）。
 * 未記録は null（呼び出し側が「記録されていません」などを出す）、自動の処理（`system`・`system:…`）は
 * 「システム」、引けない利用者（削除済みなど）は「（削除済みのユーザー）」。
 */
export function actorDisplayName(loginUserId: string | null | undefined, names: UserDisplayNames | undefined): string | null {
  const login = loginUserId?.trim();
  if (!login) return null;
  const name = names?.[login]?.trim();
  if (name) return name;
  if (login === "system" || login.startsWith("system:")) return t("user.systemActor");
  return deletedUserLabel();
}
