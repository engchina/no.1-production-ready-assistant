import type { ReactNode } from "react";
import { t } from "@/lib/i18n";
import { Disclosure } from "@production-ready/ui";

export function TechnicalDetails({
  value,
  children,
}: {
  value?: unknown;
  children?: ReactNode;
}) {
  return (
    <Disclosure surface="sunken" summary={t("ontologyUi.technicalDetails")}>
      {children ?? (
        <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words text-xs">
          {JSON.stringify(value, null, 2)}
        </pre>
      )}
    </Disclosure>
  );
}
export function fieldLabel(field: string) {
  const key = `ontologyResults.field.${field}` as Parameters<typeof t>[0];
  return t(key) === key ? field : t(key);
}
/** 値が無い（null・空文字・空の配列・中身がすべて空の object）か。詳細のパネルで「未設定」を出さないために使う（#1788）。 */
export function isEmptyDefinitionValue(value: unknown): boolean {
  if (value == null || value === "") return true;
  if (Array.isArray(value)) return value.every(isEmptyDefinitionValue);
  if (typeof value === "object") return Object.values(value).every(isEmptyDefinitionValue);
  return false;
}
export function DefinitionValue({
  value,
  names = {},
  hideEmpty = false,
}: {
  value: unknown;
  names?: Record<string, string>;
  /** true なら、入れ子の object の値の無い項目を出さない。 */
  hideEmpty?: boolean;
}) {
  if (value == null || value === "" || (Array.isArray(value) && !value.length))
    return <span>{t("ontologyResults.unspecified")}</span>;
  if (typeof value === "boolean")
    return (
      <span>{t(value ? "ontologyResults.yes" : "ontologyResults.no")}</span>
    );
  if (Array.isArray(value))
    return (
      <ul className="grid gap-1">
        {value
          .filter((item) => !hideEmpty || !isEmptyDefinitionValue(item))
          .map((item, index) => (
            <li key={index}>
              <DefinitionValue value={item} names={names} hideEmpty={hideEmpty} />
            </li>
          ))}
      </ul>
    );
  if (typeof value === "object")
    return (
      <dl className="grid gap-2">
        {Object.entries(value)
          .filter(([, item]) => !hideEmpty || !isEmptyDefinitionValue(item))
          .map(([key, item]) => (
            <div key={key}>
              <dt className="font-medium">{fieldLabel(key)}</dt>
              <dd className="pl-3">
                <DefinitionValue value={item} names={names} hideEmpty={hideEmpty} />
              </dd>
            </div>
          ))}
      </dl>
    );
  return (
    <span className="break-words [overflow-wrap:anywhere]">
      {names[String(value)] ?? String(value)}
    </span>
  );
}
const DEFINITION_HIDDEN_KEYS = ["id", "implementation_key", "origin", "review_status"];

/** ラベルと値の並び（DefinitionFields と同じ見た目）。グラフの詳細のパネルで、定義の外の項目を先に並べるのに使う。 */
export function DetailFieldList({
  items,
  testId,
}: {
  items: { key: string; label: string; value: ReactNode }[];
  testId?: string;
}) {
  if (!items.length) return null;
  return (
    <dl className="grid min-w-0 gap-2 text-sm" data-testid={testId}>
      {items.map((item) => (
        <div className="min-w-0 break-words" key={item.key}>
          <dt className="font-medium">{item.label}</dt>
          <dd>{item.value}</dd>
        </div>
      ))}
    </dl>
  );
}

export function DefinitionFields({
  definition,
  hideEmpty = false,
  omitKeys = [],
}: {
  definition: Record<string, unknown>;
  /** true なら値の無い項目（「未設定」）を出さない（グラフの詳細のパネル。#1788）。 */
  hideEmpty?: boolean;
  /** 別の所に出した項目を重ねて出さない。 */
  omitKeys?: string[];
}) {
  const entries = Object.entries(definition).filter(
    ([key, value]) =>
      !DEFINITION_HIDDEN_KEYS.includes(key) &&
      !omitKeys.includes(key) &&
      (!hideEmpty || !isEmptyDefinitionValue(value))
  );
  return (
    <DetailFieldList
      items={entries.map(([key, value]) => ({
        key,
        label: fieldLabel(key),
        value: (
          <DefinitionValue
            value={key === "kind" && typeof value === "string" ? t(`ontologyResults.kind.${value}`) : value}
            hideEmpty={hideEmpty}
          />
        ),
      }))}
    />
  );
}
