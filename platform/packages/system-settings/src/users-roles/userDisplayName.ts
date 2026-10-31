// ユーザーを画面に出す名前（#1696）。
// サイドナビのアカウント欄の 1 行目だけはログイン ID を出し、それ以外の画面はユーザーの表示名を出す。
// 表示名が空ならログイン ID、どちらも無い（削除済み・取得できない）利用者は UUID を出さずに
// 「（削除済みのユーザー）」と出す。3 製品で同じ関数を使う。
import { t } from "./messages";

export interface UserNameReference {
  display_name?: string | null;
  login_user_id?: string | null;
}

/** 削除済み・取得できない利用者の文言の既定。 */
export function deletedUserLabel(): string {
  return t("security.users.deletedUser");
}

/** ユーザーを画面に出す名前（表示名 → ログイン ID → 「（削除済みのユーザー）」）。 */
export function userDisplayName(
  user: UserNameReference | null | undefined,
  deletedLabel: string = deletedUserLabel(),
): string {
  return user?.display_name?.trim() || user?.login_user_id?.trim() || deletedLabel;
}
