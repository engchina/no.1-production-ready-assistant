import type { ParserAdapterBackend } from "@/lib/api";

/**
 * 文書解析エンジンの並びの正本（#3359）。文書解析の画面の使用エンジンのカードと、処理レシピの
 * 「文書解析」の選択肢が同じ順にする（Docling・Unstructured。#21）。
 */
export const PARSER_BACKEND_ORDER = [
  "docling",
  "unstructured",
] as const satisfies readonly ParserAdapterBackend[];

/** 正本の順に並べる（正本に無いエンジンは元の順のまま後ろへ）。 */
export function sortParserBackends<T extends string>(backends: readonly T[]): T[] {
  const order = PARSER_BACKEND_ORDER as readonly string[];
  const rank = (backend: T) => {
    const index = order.indexOf(backend);
    return index === -1 ? order.length : index;
  };
  return backends
    .map((backend, index) => ({ backend, index }))
    .sort((a, b) => rank(a.backend) - rank(b.backend) || a.index - b.index)
    .map(({ backend }) => backend);
}
