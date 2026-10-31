import { Fragment } from "react";

import { identifierWrapSegments } from "@/lib/format";

/**
 * DB 識別子を `.` / `_` の位置で優先的に折り返すテキスト。
 * 文字の途中で分断すると `ADMIN.DENPYO_ACTIVITY_LO` / `G` のように読めなくなるため、
 * 区切りの直後に `<wbr>` を挟み、区切りのない長い区間だけ `overflow-wrap:anywhere` で折り返す。
 * コピー・検索・読み上げのテキストは元の識別子のまま。
 */
export function IdentifierText({
  value,
  className = "",
  "data-testid": testId,
}: {
  value: string;
  className?: string;
  "data-testid"?: string;
}) {
  const segments = identifierWrapSegments(value);
  return (
    <span className={`[overflow-wrap:anywhere] ${className}`.trim()} data-testid={testId}>
      {segments.map((segment, index) => (
        <Fragment key={index}>
          {index > 0 && <wbr />}
          {segment}
        </Fragment>
      ))}
    </span>
  );
}
