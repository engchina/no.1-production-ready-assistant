import {expect,test} from "./fixtures/test";
import {mockDatabaseReady,mockLocalAuth} from "./_helpers";
test.beforeEach(async({page})=>{await mockLocalAuth(page);await mockDatabaseReady(page);});

test("軌跡の本文記録を既定の無効から切り替えて保存する", { tag: "@responsive" }, async ({ page }, testInfo) => {
  let enabled=false;
  const saved:boolean[]=[];
  await page.route("**/api/settings/trace-recording",async route=>{
    if(route.request().method()==="PUT") { enabled=route.request().postDataJSON().enabled;saved.push(enabled); }
    await route.fulfill({json:{data:{enabled,updated_at:null},error_messages:[],warning_messages:[]}});
  });
  await page.goto("/settings/trace-recording");
  await expect(page.getByRole("heading",{name:"軌跡の記録",level:1})).toBeVisible();
  const toggle=page.getByRole("switch",{name:"入力・結果の本文を軌跡に記録する"});
  await expect(toggle).toHaveAttribute("aria-checked","false");
  await expect(page.getByText("通常の質問・回答の履歴や業務の結果は変わりません。既存の軌跡の本文も削除しません。",{exact:true})).toBeVisible();
  for(const state of [true,false]) {
    await toggle.focus();await page.keyboard.press("Space");
    await expect(toggle).toHaveAttribute("aria-checked",String(state));
    await page.getByRole("button",{name:"保存",exact:true}).click();
    await expect.poll(()=>enabled).toBe(state);
    await expect(page.getByRole("button",{name:"保存",exact:true})).toBeEnabled();
    await page.reload();await expect(toggle).toHaveAttribute("aria-checked",String(state));
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
    for(const theme of ["light","dark"]) {
      await page.evaluate(value=>{document.documentElement.dataset.theme=value;document.documentElement.classList.remove("light","dark");},theme);
      await page.screenshot({path:testInfo.outputPath(`trace-recording-${state?"on":"off"}-${theme}.png`),fullPage:true});
    }
  }
  expect(saved).toEqual([true,false]);
});
