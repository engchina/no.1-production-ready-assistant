import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { expect, it } from "vitest";
import { OutputResultTable, ResultTable } from "./ResultTables";

const data={columns:[{name:"value"}],rows:[[null]],truncated:false,totalRowCount:1,elapsedMs:null};
it("出力の空値は記録なし、通常の結果の表はNULLを維持する",()=>{
  expect(renderToStaticMarkup(createElement(OutputResultTable,{data}))).toContain("記録なし");
  expect(renderToStaticMarkup(createElement(ResultTable,{data,name:"結果"}))).toContain("NULL");
});
