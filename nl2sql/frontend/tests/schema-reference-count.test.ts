import assert from "node:assert/strict";
import test from "node:test";

import { schemaReferenceListCount } from "../src/features/nl2sql/schemaReferenceCount.ts";

const base = {
  loading: false,
  searching: false,
  hasMore: false,
  scoped: false,
  shown: 0,
  scopedTotal: 0,
  serverSearch: true,
};

function counts(input: Partial<Parameters<typeof schemaReferenceListCount>[0]>) {
  const result = schemaReferenceListCount({ ...base, ...input });
  return result ? { total: result.total, matched: result.matched } : undefined;
}

test("スキーマ参照: 続きがあっても API の total で「全 N 件」を出す（業務プロファイルを API が絞るとき。#1891）", () => {
  // SQL 生成の画面は profile_id を API に渡すので、total は業務プロファイルの対象で絞った件数。
  assert.deepEqual(
    counts({ hasMore: true, scoped: true, serverScoped: true, shown: 50, scopedTotal: 50, totalCount: 120 }),
    { total: 120, matched: undefined }
  );
  // 画面の側だけで絞るときは、API の total が表示と合わないので出さない（「さらに読み込む」が続きを示す）。
  assert.equal(counts({ hasMore: true, scoped: true, shown: 50, scopedTotal: 50, totalCount: 120 }), undefined);
  // total が無い（旧 backend・続きのページ）ときも出さない。
  assert.equal(counts({ hasMore: true, scoped: true, serverScoped: true, shown: 50, totalCount: null }), undefined);
});

test("スキーマ参照: 検索中は「N 件（全 M 件）」、M は検索語の無い問い合わせの total（#1891）", () => {
  assert.deepEqual(
    counts({
      searching: true,
      hasMore: true,
      scoped: true,
      serverScoped: true,
      shown: 50,
      totalCount: 64,
      unfilteredTotalCount: 120,
    }),
    { total: 120, matched: 64 }
  );
  // 一致が 1 ページに収まるときは表示している数が N。
  assert.deepEqual(
    counts({ searching: true, scoped: true, serverScoped: true, shown: 3, totalCount: 3, unfilteredTotalCount: 120 }),
    { total: 120, matched: 3 }
  );
  // M が分からない間は件数を出さない（「3 件」だけにして全件と取り違えさせない）。
  assert.equal(counts({ searching: true, shown: 3, totalCount: 3, unfilteredTotalCount: null }), undefined);
  // 読み込み中は出さない。
  assert.equal(counts({ loading: true, shown: 3 }), undefined);
});

test("スキーマ参照: 続きが無いときは表示している表の数", () => {
  assert.deepEqual(counts({ shown: 7, scopedTotal: 7, totalCount: 7 }), { total: 7, matched: undefined });
  // 画面の側で検索するとき（API に検索語を渡さない）は、読み込んだ表が絞り込む前の全件。
  assert.deepEqual(
    counts({ searching: true, serverSearch: false, shown: 2, scopedTotal: 7 }),
    { total: 7, matched: 2 }
  );
});
