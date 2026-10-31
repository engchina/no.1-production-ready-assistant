// @vitest-environment happy-dom
import { ConfirmProvider } from "@production-ready/ui";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { expect, it, vi } from "vitest";
import { TraceRecordingSettingsPage } from "../src";

(globalThis as {IS_REACT_ACT_ENVIRONMENT?:boolean}).IS_REACT_ACT_ENVIRONMENT=true;
it("既定は無効で、本文とメタデータの範囲を説明し、保存した値を反映する", async()=>{
 const host=document.createElement("div");document.body.append(host);const root=createRoot(host);
 const update=vi.fn(async ({enabled}:{enabled:boolean})=>({enabled}));
 try {
  await act(async()=>root.render(<MemoryRouter><QueryClientProvider client={new QueryClient({defaultOptions:{queries:{retry:false}}})}><ConfirmProvider><TraceRecordingSettingsPage api={{get:async()=>({enabled:false}),update}} /></ConfirmProvider></QueryClientProvider></MemoryRouter>));
  for(let i=0;i<10;i++) await act(async()=>{await new Promise(resolve=>setTimeout(resolve,0));});
  const toggle=host.querySelector<HTMLButtonElement>('[role="switch"]')!;
  expect(toggle.getAttribute("aria-checked")).toBe("false");expect(host.textContent).toContain("Agent・RAG・NL2SQL");expect(host.textContent).toContain("embedding");expect(host.textContent).toContain("削除しません");
  await act(async()=>toggle.click());
  await act(async()=>host.querySelector("form")!.dispatchEvent(new Event("submit",{bubbles:true,cancelable:true})));
  expect(update).toHaveBeenCalledWith({enabled:true});
 }finally{act(()=>root.unmount());host.remove();}
});

it("取得失敗から再試行できる", async () => {
 const host=document.createElement("div");document.body.append(host);const root=createRoot(host);
 const get=vi.fn().mockRejectedValueOnce(new Error("synthetic")).mockResolvedValue({enabled:false});
 const update=vi.fn();
 const flush=async()=>{for(let i=0;i<10;i++)await act(async()=>{await new Promise(resolve=>setTimeout(resolve,0));});};
 try {
  await act(async()=>root.render(<MemoryRouter><QueryClientProvider client={new QueryClient({defaultOptions:{queries:{retry:false}}})}><ConfirmProvider><TraceRecordingSettingsPage api={{get,update}} /></ConfirmProvider></QueryClientProvider></MemoryRouter>));
  await flush();
  expect(host.textContent).toContain("軌跡の記録設定を取得できませんでした。");
  expect(host.querySelector("form")).toBeNull();
  const retry=[...host.querySelectorAll("button")].find(button=>button.textContent?.includes("再試行"))!;
  await act(async()=>retry.click());await flush();
  expect(host.querySelector('[role="switch"]')?.getAttribute("aria-checked")).toBe("false");
  expect(update).not.toHaveBeenCalled();
 }finally{act(()=>root.unmount());host.remove();}
});

it("保存中は変更を抑止し、失敗した値を保持して再保存できる", async () => {
 const host=document.createElement("div");document.body.append(host);const root=createRoot(host);
 let reject!:(reason:Error)=>void;
 const update=vi.fn().mockImplementationOnce(()=>new Promise((_resolve,fail)=>{reject=fail;})).mockImplementation(async({enabled}:{enabled:boolean})=>({enabled}));
 const flush=async()=>{for(let i=0;i<10;i++)await act(async()=>{await new Promise(resolve=>setTimeout(resolve,0));});};
 const submit=async()=>{await act(async()=>{host.querySelector("form")!.dispatchEvent(new Event("submit",{bubbles:true,cancelable:true}));});await flush();};
 try {
  await act(async()=>root.render(<MemoryRouter><QueryClientProvider client={new QueryClient({defaultOptions:{queries:{retry:false}}})}><ConfirmProvider><TraceRecordingSettingsPage api={{get:async()=>({enabled:false}),update}} /></ConfirmProvider></QueryClientProvider></MemoryRouter>));await flush();
  const toggle=host.querySelector<HTMLButtonElement>('[role="switch"]')!;
  await act(async()=>toggle.click());await submit();
  expect(host.querySelector("fieldset")!.disabled).toBe(true);
  await act(async()=>reject(new Error("synthetic")));await flush();
  expect(host.textContent).toContain("軌跡の記録設定を保存できませんでした。");
  expect(toggle.getAttribute("aria-checked")).toBe("true");
  expect(host.querySelector("fieldset")!.disabled).toBe(false);
  await submit();expect(update).toHaveBeenCalledTimes(2);
 }finally{act(()=>root.unmount());host.remove();}
});
