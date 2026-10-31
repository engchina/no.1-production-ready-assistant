import { SelectField, type SelectFieldOption } from "@production-ready/ui";

import type { DocumentCategory } from "@/lib/api";
import { t } from "@/lib/i18n";
import { useDocumentCategories } from "@/lib/queries";

export const CATEGORY_KEYS = ["large_category", "middle_category", "small_category"] as const;
export type CategoryKey = (typeof CATEGORY_KEYS)[number];
export type CategoryValues = Record<CategoryKey, string>;

/** 番号の接頭辞（`10_`）を除いた名前。検索の絞り込み・backend の `category_label` と同じ比べ方。 */
export function categoryLabel(value: string): string {
  return value.normalize("NFKC").trim().replace(/^[0-9]+_(?=.)/, "");
}

function findByName(nodes: readonly DocumentCategory[], name: string): DocumentCategory | undefined {
  const label = categoryLabel(name);
  return label ? nodes.find((node) => categoryLabel(node.name) === label) : undefined;
}

function options(nodes: readonly DocumentCategory[], current: string): SelectFieldOption[] {
  const listed = nodes.map((node) => ({ value: node.name, label: node.name }));
  // 一覧に無い保存済みの値（一覧を作る前に保存した値など）も、今の値として選べるように残す。
  return current && !findByName(nodes, current) ? [{ value: current, label: current }, ...listed] : listed;
}

/**
 * 文書の分類（大分類 → 中分類 → 小分類）を、分類の一覧から選ぶ 3 つの選択欄（#2131）。文書の詳細と RAG 検索で
 * 共通に使う。どれも必須ではなく「指定なし」に戻せる。中分類は選んだ大分類の下、小分類は選んだ中分類の下から
 * 選び、上の段を変えたら下の段は「指定なし」に戻す。
 */
export function CategorySelectFields({
  idPrefix,
  labels,
  values,
  onChange,
  disabled,
}: {
  idPrefix: string;
  labels: Record<CategoryKey, string>;
  values: CategoryValues;
  onChange: (values: CategoryValues) => void;
  disabled?: boolean;
}) {
  const tree = useDocumentCategories().data ?? [];
  const large = findByName(tree, values.large_category);
  const middle = large ? findByName(large.children, values.middle_category) : undefined;
  const levels: { key: CategoryKey; nodes: readonly DocumentCategory[]; parentSelected: boolean }[] = [
    { key: "large_category", nodes: tree, parentSelected: true },
    { key: "middle_category", nodes: large?.children ?? [], parentSelected: Boolean(values.large_category) },
    { key: "small_category", nodes: middle?.children ?? [], parentSelected: Boolean(values.middle_category) },
  ];
  const update = (key: CategoryKey, value: string) => {
    const next = { ...values, [key]: value };
    // 上の段を変えたら、下の段は選び直す（別の大分類の中分類を残さない）。
    if (key === "large_category") Object.assign(next, { middle_category: "", small_category: "" });
    if (key === "middle_category") next.small_category = "";
    onChange(next);
  };
  return (
    <>
      {levels.map(({ key, nodes, parentSelected }) => (
        <SelectField
          key={key}
          id={`${idPrefix}-${key}`}
          width="md"
          label={labels[key]}
          value={values[key]}
          options={options(nodes, values[key])}
          emptyOptionLabel={t("documentCategories.select.none")}
          placeholder={t("documentCategories.select.none")}
          helper={
            key !== "large_category" && !parentSelected && !values[key]
              ? t(`documentCategories.select.parentFirst.${key}`)
              : undefined
          }
          disabled={disabled || (!parentSelected && !values[key])}
          onValueChange={(value) => update(key, value)}
        />
      ))}
    </>
  );
}
