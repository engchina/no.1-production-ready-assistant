// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { AnswerSections, ChatAnswerBody } from "../src";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let host: HTMLDivElement;
let root: Root;

beforeEach(() => {
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
});

describe("ChatAnswerBody（#3433）", () => {
  it("回答の本文の前に見出し「回答」（h3）を出し、本文の領域の名前にする", () => {
    act(() =>
      root.render(
        <ChatAnswerBody testId="body">
          <p>本文の段落</p>
        </ChatAnswerBody>
      )
    );
    const body = host.querySelector('[data-testid="body"]')!;
    expect(body.tagName).toBe("SECTION");
    const heading = body.querySelector('[data-testid="chat-answer-heading"]')!;
    expect(heading.tagName).toBe("H3");
    expect(heading.textContent).toBe("回答");
    expect(body.getAttribute("aria-labelledby")).toBe(heading.id);
    // 見出しは本文の前。
    expect(body.firstElementChild).toBe(heading);
    expect(body.textContent).toBe("回答本文の段落");
  });

  it("見出しの文言と段を変えられる", () => {
    act(() =>
      root.render(
        <ChatAnswerBody headingLevel={4} labels={{ heading: "Answer" }} testId="body">
          <p>text</p>
        </ChatAnswerBody>
      )
    );
    const heading = host.querySelector('[data-testid="chat-answer-heading"]')!;
    expect(heading.tagName).toBe("H4");
    expect(heading.textContent).toBe("Answer");
  });

  it("見出しは本文の中の節の見出しと同じ書式（同じ class）で描く", () => {
    act(() =>
      root.render(
        <ChatAnswerBody>
          <AnswerSections
            blocks={[
              {
                kind: "section",
                key: "s",
                title: "確認できた事項",
                entries: [{ key: "e", content: "項目" }],
              },
            ]}
          />
        </ChatAnswerBody>
      )
    );
    const heading = host.querySelector('[data-testid="chat-answer-heading"]')!;
    const sectionHeading = host.querySelector('[data-testid="answer-section"] h4')!;
    expect(sectionHeading.textContent).toBe("確認できた事項");
    expect(heading.className).toBe(sectionHeading.className);
  });
});
