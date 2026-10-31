/**
 * oxlint の JS プラグイン: デザインシステム遵守ルール（`design-system/restricted-syntax`）。
 *
 * oxlint のネイティブルールには ESLint の `no-restricted-syntax` が無い。そこで、同じ
 * `{ selector, message }` 形式（esquery セレクタ）のオプションを受け取るルールをここで提供する。
 * adherence.oxlintrc.json はこのファイルを相対パスで読み込むので、アプリはこのファイルをコピーせず、
 * adherence.oxlintrc.json を `extends` するだけでよい（AGENTS.md「lint」節）。
 *
 * ESLint を使うアプリは、このプラグインを使わない。同じオプションを ESLint 標準の
 * `no-restricted-syntax` にそのまま渡す（ARCHITECTURE.md §5）。
 *
 * 由来: NL2SQL #532 の frontend/lint/design-system-plugin.mjs。
 */
const restrictedSyntax = {
  meta: {
    type: "problem",
    docs: { description: "ESLint の no-restricted-syntax 相当（esquery セレクタで構文を禁止する）" },
    schema: false,
  },
  create(context) {
    const visitor = {};
    for (const { selector, message } of context.options) {
      const previous = visitor[selector];
      visitor[selector] = (node) => {
        previous?.(node);
        context.report({ node, message });
      };
    }
    return visitor;
  },
};

export default {
  meta: { name: "design-system" },
  rules: { "restricted-syntax": restrictedSyntax },
};
