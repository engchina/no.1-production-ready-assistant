import { Banner } from "@production-ready/ui";

/**
 * API の応答が返す警告（取込・プレビュー・生成・詳細の warnings）を、1 つの warning の Banner にまとめて出す。
 * 手書きの warning の面（アイコン・role 無し）を使わない（UX 契約 messaging.md §10.2、#724）。
 * 警告が無いときは何も描画しない（空の面を作らない。§9 P4）。
 */
export function WarningsBanner({
  warnings,
  className,
}: {
  warnings?: readonly string[] | null;
  className?: string;
}) {
  const items = (warnings ?? []).filter(Boolean);
  if (items.length === 0) return null;
  return (
    <Banner severity="warning" className={className}>
      {items.length === 1 ? (
        items[0]
      ) : (
        <ul className="grid list-disc gap-1 pl-5">
          {items.map((warning) => (
            <li key={warning} className="break-words [overflow-wrap:anywhere]">
              {warning}
            </li>
          ))}
        </ul>
      )}
    </Banner>
  );
}
