/**
 * NL2SQL 固有のデザインシステム遵守ルール用のプラグイン（`nl2sql-design-system/restricted-syntax`）。
 *
 * ルールの実装は platform の design-system-plugin.mjs をそのまま使う（コピーしない）。
 * adherence.oxlintrc.json が同じプラグインを `design-system` として読み込むため、
 * アプリ固有のセレクタで adherence のルール設定を上書きしないよう、別名のプラグインとして再公開する。
 */
import platformPlugin from "../../../platform/docs/design-system/design-system-plugin.mjs";

export default {
  meta: { name: "nl2sql-design-system" },
  rules: platformPlugin.rules,
};
