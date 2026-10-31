import type { InheritedAnswerContactData } from "@/lib/api";

/**
 * 回答の窓口の案内の継承（#2683）。決め方の正本は backend の `app/rag/answer_notice.py`:
 *
 * 1. 業務ガイドの人への引き継ぎで、ガイドに窓口（`handoff.contact`）があれば、回答の本文がその窓口を案内し、
 *    回答の注意書きには窓口を出さない。
 * 2. 検索・回答プロファイルの窓口の上書き（`contact_override`）があれば、その窓口。無ければシステム設定の窓口。
 * 3. システム設定の回答の注意書きが無効なら、注意書き（窓口の案内を含む）を出さない。
 *
 * 画面は空の欄に継承する窓口を placeholder で示し、有効な窓口と出どころを欄の下に出す（値は欄にコピーしない）。
 * ガイドは 1 つの検索・回答プロファイルに属する（`search_answer_profile_id`）ので、継承元はそのプロファイル。
 */

/** 窓口の出どころ。`none` は回答の注意書きが無効で、窓口の案内を出さない。 */
export type ContactSource = "guide" | "profile" | "system" | "none";

export interface ResolvedContact {
  source: ContactSource;
  /** 窓口（`none` なら空）。 */
  contact: string;
}

export interface ContactInheritance {
  /** 欄が空のときに継承する窓口。システム設定を読めていなければ null。 */
  inherited: ResolvedContact | null;
  /** いまの入力で回答に使う窓口。システム設定を読めていなければ null。 */
  effective: ResolvedContact | null;
  /** 入力が継承する窓口と同じなら、その出どころ（空にしても同じ窓口になる）。 */
  sameAs: Exclude<ContactSource, "guide" | "none"> | null;
}

/** 窓口は 1 行（改行・連続の空白を 1 つの空白にそろえる。backend の `normalize_notice_contact` と同じ）。 */
export function normalizeContact(value: string | null | undefined): string {
  return (value ?? "").split(/\s+/).filter(Boolean).join(" ");
}

function systemContact(system: InheritedAnswerContactData | undefined): ResolvedContact | null {
  if (!system) return null;
  const contact = normalizeContact(system.contact);
  return system.enabled && contact ? { source: "system", contact } : { source: "none", contact: "" };
}

function sameAs(value: string, inherited: ResolvedContact | null): ContactInheritance["sameAs"] {
  if (!value || !inherited) return null;
  if (inherited.source !== "profile" && inherited.source !== "system") return null;
  return inherited.contact === value ? inherited.source : null;
}

/** 検索・回答プロファイルの「窓口の上書き」（入力中の値）の継承と有効な窓口。 */
export function profileContactInheritance(
  contactOverride: string,
  system: InheritedAnswerContactData | undefined,
): ContactInheritance {
  const inherited = systemContact(system);
  const value = normalizeContact(contactOverride);
  let effective: ResolvedContact | null = inherited;
  // 注意書きが無効なら、上書きがあっても窓口の案内は出ない。
  if (inherited && inherited.source !== "none" && value) effective = { source: "profile", contact: value };
  return { inherited, effective, sameAs: sameAs(value, inherited) };
}

/**
 * 業務ガイドの「引き継ぎ先」（入力中の値）の継承と有効な窓口。継承元はガイドが属するプロファイルの保存済みの
 * 窓口の上書き（回答に使う値）、無ければシステム設定の窓口。
 */
export function guideContactInheritance(
  handoffContact: string,
  profileContactOverride: string | null | undefined,
  system: InheritedAnswerContactData | undefined,
): ContactInheritance {
  const fromSystem = systemContact(system);
  const override = normalizeContact(profileContactOverride);
  const inherited: ResolvedContact | null =
    fromSystem && fromSystem.source !== "none" && override ? { source: "profile", contact: override } : fromSystem;
  const value = normalizeContact(handoffContact);
  // ガイドの窓口は回答の本文で案内するので、注意書きが無効でも使われる。
  const effective: ResolvedContact | null = value ? { source: "guide", contact: value } : inherited;
  return { inherited, effective, sameAs: sameAs(value, inherited) };
}
