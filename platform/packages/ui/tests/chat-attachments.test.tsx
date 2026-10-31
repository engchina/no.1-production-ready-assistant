// @vitest-environment happy-dom
import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ChatAttachmentList,
  ChatComposer,
  ChatUserMessage,
  formatAttachmentSize,
  useChatAttachments,
  type ChatAttachmentUploadResult,
} from "../src";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let host: HTMLDivElement;
let root: Root;

const messages = {
  tooLarge: (max: string) => `${max} まで`,
  tooMany: (max: number) => `${max} 件まで`,
  unsupported: "添付できない種類",
  uploadFailed: () => "添付できませんでした",
};

type Upload = (file: File) => Promise<ChatAttachmentUploadResult>;

function Harness({
  upload,
  remove,
  onSubmit,
  exposeIds,
}: {
  upload: Upload;
  remove: (id: string) => Promise<unknown>;
  onSubmit: () => void;
  exposeIds?: (ids: string[]) => void;
}) {
  const [value, setValue] = useState("質問");
  const attachments = useChatAttachments({
    upload,
    remove,
    maxBytes: 1024,
    maxFiles: 2,
    accept: [".pdf", ".csv"],
    messages,
    formatSize: formatAttachmentSize,
  });
  exposeIds?.(attachments.readyIds);
  return (
    <ChatComposer
      id="composer"
      value={value}
      onValueChange={setValue}
      onSubmit={onSubmit}
      onStop={vi.fn()}
      running={false}
      label="質問"
      sendLabel="送信"
      stopLabel="停止"
      sendTestId="send"
      attachments={{
        items: attachments.items,
        onAddFiles: attachments.add,
        onRemove: attachments.remove,
        accept: ".pdf,.csv",
        hint: "添付は 24 時間で消えます",
        labels: {
          add: "ファイルを添付",
          drop: "ここにドロップして添付",
          list: "添付ファイル",
          remove: (name) => `${name} を外す`,
          uploading: "アップロード中",
        },
      }}
    />
  );
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const file = (name: string, size = 10) => new File(["x".repeat(size)], name);
const chips = () => Array.from(host.querySelectorAll<HTMLLIElement>("[data-chat-attachment]"));
const send = () => host.querySelector<HTMLButtonElement>('[data-testid="send"]')!;

function choose(files: File[]) {
  const input = host.querySelector<HTMLInputElement>('[data-testid="chat-attachments-input"]')!;
  Object.defineProperty(input, "files", { value: files, configurable: true });
  act(() => {
    input.dispatchEvent(new Event("change", { bubbles: true }));
  });
}

beforeEach(() => {
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
});

describe("ChatComposer の添付", () => {
  it("選んだファイルをアップロード中にし、終わるまで送れない。終われば readyIds に入る", async () => {
    const pending = deferred<ChatAttachmentUploadResult>();
    const upload = vi.fn<Upload>(() => pending.promise);
    let ids: string[] = [];
    act(() =>
      root.render(
        <Harness upload={upload} remove={vi.fn()} onSubmit={vi.fn()} exposeIds={(value) => (ids = value)} />
      )
    );
    expect(host.querySelector('[data-testid="chat-attachments-add"]')?.textContent).toBe("ファイルを添付");
    choose([file("a.pdf")]);
    expect(upload).toHaveBeenCalledTimes(1);
    expect(chips()[0]?.dataset.status).toBe("uploading");
    expect(chips()[0]?.textContent).toContain("アップロード中");
    expect(send().disabled || send().getAttribute("aria-disabled") === "true").toBe(true);
    await act(async () => {
      pending.resolve({ attachmentId: "att-1", fileName: "a.pdf", sizeBytes: 10, kind: "pdf" });
      await pending.promise;
    });
    expect(chips()[0]?.dataset.status).toBe("ready");
    expect(ids).toEqual(["att-1"]);
    expect(send().disabled).toBe(false);
  });

  it("種類・大きさ・件数の上限は送る前に失敗にし、理由を出す", async () => {
    const upload = vi.fn<Upload>(async (f) => ({ attachmentId: f.name, fileName: f.name, sizeBytes: f.size }));
    act(() => root.render(<Harness upload={upload} remove={vi.fn()} onSubmit={vi.fn()} />));
    await act(async () => {
      choose([file("a.exe"), file("big.pdf", 2048), file("1.csv"), file("2.csv"), file("3.csv")]);
    });
    const states = chips().map((chip) => [chip.dataset.status, chip.querySelector('[role="alert"]')?.textContent]);
    expect(states).toEqual([
      ["failed", "添付できない種類"],
      ["failed", "1 KB まで"],
      ["ready", undefined],
      ["ready", undefined],
      ["failed", "2 件まで"],
    ]);
    expect(upload).toHaveBeenCalledTimes(2);
  });

  it("外すと API でも消し、アップロード中に外したものは終わった後に消す", async () => {
    const pending = deferred<ChatAttachmentUploadResult>();
    const upload = vi.fn<Upload>(() => pending.promise);
    const remove = vi.fn(async () => undefined);
    act(() => root.render(<Harness upload={upload} remove={remove} onSubmit={vi.fn()} />));
    choose([file("a.pdf")]);
    const button = host.querySelector<HTMLButtonElement>('button[aria-label="a.pdf を外す"]')!;
    act(() => button.click());
    expect(chips()).toHaveLength(0);
    await act(async () => {
      pending.resolve({ attachmentId: "att-9", fileName: "a.pdf", sizeBytes: 10 });
      await pending.promise;
    });
    expect(remove).toHaveBeenCalledWith("att-9");
  });

  it("API の失敗は理由を出し、送信は止めない", async () => {
    const upload = vi.fn<Upload>(async () => {
      throw new Error("413");
    });
    act(() => root.render(<Harness upload={upload} remove={vi.fn()} onSubmit={vi.fn()} />));
    await act(async () => {
      choose([file("a.pdf")]);
    });
    expect(chips()[0]?.dataset.status).toBe("failed");
    expect(chips()[0]?.textContent).toContain("添付できませんでした");
    expect(send().disabled).toBe(false);
  });

  it("ファイルを重ねると案内を出し、ドロップで添付する", async () => {
    const upload = vi.fn<Upload>(async (f) => ({ attachmentId: f.name, fileName: f.name, sizeBytes: f.size }));
    act(() => root.render(<Harness upload={upload} remove={vi.fn()} onSubmit={vi.fn()} />));
    const zone = host.querySelector<HTMLDivElement>('[data-testid="chat-attachments-dropzone"]')!;
    const dropped = [file("a.pdf")];
    const dataTransfer = { types: ["Files"], files: dropped, dropEffect: "none" };
    const dragEvent = (type: string) => {
      const event = new Event(type, { bubbles: true, cancelable: true });
      Object.defineProperty(event, "dataTransfer", { value: dataTransfer });
      return event;
    };
    act(() => {
      zone.dispatchEvent(dragEvent("dragenter"));
    });
    expect(host.querySelector('[data-testid="chat-attachments-drop-hint"]')?.textContent).toBe(
      "ここにドロップして添付"
    );
    await act(async () => {
      zone.dispatchEvent(dragEvent("drop"));
    });
    expect(host.querySelector('[data-testid="chat-attachments-drop-hint"]')).toBeNull();
    expect(upload).toHaveBeenCalledWith(dropped[0]);
  });
});

describe("添付の表示", () => {
  it("発話の下に右寄せで、種類と大きさを出す", () => {
    act(() =>
      root.render(
        <ChatUserMessage
          attachments={[{ id: "1", fileName: "表.xlsx", sizeBytes: 1536, kind: "excel" }]}
          attachmentsLabel="添付ファイル"
        >
          質問
        </ChatUserMessage>
      )
    );
    const list = host.querySelector("ul[aria-label='添付ファイル']")!;
    expect(list.className).toContain("justify-end");
    expect(list.textContent).toContain("表.xlsx");
    expect(list.textContent).toContain("2 KB");
    expect(list.querySelector("button")).toBeNull();
  });

  it("空なら何も出さない", () => {
    act(() => root.render(<ChatAttachmentList items={[]} labels={{ list: "添付" }} />));
    expect(host.innerHTML).toBe("");
  });

  it("大きさを KB・MB で出す", () => {
    expect([formatAttachmentSize(500), formatAttachmentSize(1536), formatAttachmentSize(1.5 * 1024 * 1024)]).toEqual([
      "500 B",
      "2 KB",
      "1.5 MB",
    ]);
  });
});
