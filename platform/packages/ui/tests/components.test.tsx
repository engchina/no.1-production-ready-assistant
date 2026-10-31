import { KeyRound, RefreshCw, Upload } from "lucide-react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { AppShell } from "../src/components/app-shell/AppShell";
import { PageBody, Section } from "../src/components/app-shell/PageBody";
import { SidebarAccountFooter } from "../src/components/app-shell/Sidebar";
import { nextMenuIndex, orderActions, PageHeader, splitCompactActions } from "../src/components/app-shell/PageHeader";
import { StatusBadge } from "../src/components/data/status-badge";
import { Button } from "../src/components/ui/button";
import { RequiredBadge } from "../src/components/ui/required-badge";
import { SelectField } from "../src/components/ui/select-field";
import { nextTabId, TabPanel, Tabs } from "../src/components/ui/tabs";
import { TextField } from "../src/components/ui/text-field";

describe("Tabs", () => {
  const items = [
    { id: "all", label: "すべて" },
    { id: "off", label: "無効", disabled: true },
    { id: "failed", label: "失敗", count: 6 },
  ];

  it("← → は無効タブを飛ばして端で循環し、Home / End は端へ移る", () => {
    expect(nextTabId(items, "all", "ArrowRight")).toBe("failed");
    expect(nextTabId(items, "failed", "ArrowRight")).toBe("all");
    expect(nextTabId(items, "all", "ArrowLeft")).toBe("failed");
    expect(nextTabId(items, "failed", "Home")).toBe("all");
    expect(nextTabId(items, "all", "End")).toBe("failed");
    expect(nextTabId(items, "all", "Enter")).toBeNull();
  });

  it("選択中だけが Tab 順に入り（roving tabIndex）、パネルと aria-controls で結ばれる", () => {
    const html = renderToStaticMarkup(<Tabs items={items} value="failed" ariaLabel="表示" />);
    expect(html).toContain('role="tablist"');
    expect(html).toMatch(/id="pr-tab-failed"[^>]*aria-selected="true"[^>]*aria-controls="pr-panel-failed"[^>]*tabindex="0"/);
    expect(html).toMatch(/id="pr-tab-all"[^>]*aria-selected="false"[^>]*tabindex="-1"/);
  });
});

describe("Tabs のエラーの表示（#542）", () => {
  it("invalid のタブはアイコンと読み上げの説明を持ち、件数バッジと並べて説明に結ぶ", () => {
    const html = renderToStaticMarkup(
      <Tabs
        idPrefix="conn"
        value="a"
        items={[
          { id: "a", label: "プライマリ接続" },
          { id: "b", label: "セカンダリ接続", badge: "未保存", invalid: true },
        ]}
      />
    );
    const tabs = html.match(/<button[^>]*role="tab"[^>]*>/g) ?? [];
    expect(tabs[0]).not.toContain("data-invalid");
    expect(tabs[0]).not.toContain("aria-describedby");
    expect(tabs[1]).toContain('data-invalid="true"');
    expect(tabs[1]).toContain('aria-describedby="conn-tab-b-badge conn-tab-b-invalid"');
    expect(html).toContain('<span id="conn-tab-b-invalid" class="sr-only">入力にエラーがあります</span>');
    expect(html).toMatch(/<svg[^>]*text-danger-fg[^>]*aria-hidden="true"/);
  });

  it("invalidLabel で説明の文言を上書きできる", () => {
    const html = renderToStaticMarkup(
      <Tabs value="a" items={[{ id: "a", label: "A", invalid: true, invalidLabel: "未入力の欄があります" }]} />
    );
    expect(html).toContain('aria-describedby="pr-tab-a-invalid"');
    expect(html).toContain(">未入力の欄があります</span>");
  });
});

describe("Tabs でペインの中の見方を切り替える（#396）", () => {
  const items = [
    { id: "original", label: "処理前" },
    { id: "prepared", label: "処理後", disabled: true, disabledReason: "処理後ファイルはありません。" },
  ];

  it("tablist / tab と aria-selected で選択を伝え、無効のタブは Tab 順から外れて理由を title で持つ", () => {
    const html = renderToStaticMarkup(<Tabs items={items} value="original" ariaLabel="原本プレビュー" idPrefix="preview" />);
    expect(html).toContain('role="tablist" aria-label="原本プレビュー"');
    const tabs = html.match(/<button[^>]*role="tab"[^>]*>/g) ?? [];
    expect(tabs).toHaveLength(2);
    expect(tabs[0]).toContain('aria-selected="true"');
    expect(tabs[0]).toContain('tabindex="0"');
    expect(tabs[0]).toContain('aria-controls="preview-panel-original"');
    expect(tabs[0]).not.toContain("title=");
    expect(tabs[1]).toContain('aria-selected="false"');
    expect(tabs[1]).toContain('tabindex="-1"');
    expect(tabs[1]).toContain('disabled=""');
    expect(tabs[1]).toContain('title="処理後ファイルはありません。"');
  });

  it("disabledReason は有効なタブには付けない（ホバーの吹き出しを出さない）", () => {
    const html = renderToStaticMarkup(
      <Tabs items={[{ id: "a", label: "A" }, { id: "b", label: "B", disabledReason: "理由" }]} value="a" />
    );
    expect(html).not.toContain("title=");
  });

  it("矢印キーは無効のタブを飛ばし、有効なタブが 1 つだけなら移らない", () => {
    expect(nextTabId(items, "original", "ArrowRight")).toBe("original");
    expect(nextTabId(items, "original", "End")).toBe("original");
    const enabled = items.map((item) => ({ ...item, disabled: false }));
    expect(nextTabId(enabled, "original", "ArrowRight")).toBe("prepared");
    expect(nextTabId(enabled, "prepared", "ArrowRight")).toBe("original");
    expect(nextTabId(enabled, "prepared", "Home")).toBe("original");
  });

  it("TabPanel は選択中のタブと aria-labelledby で結ばれ、選択中のときだけ描く", () => {
    const html = renderToStaticMarkup(
      <>
        <TabPanel id="original" value="original" idPrefix="preview">原本</TabPanel>
        <TabPanel id="prepared" value="original" idPrefix="preview">処理後</TabPanel>
      </>
    );
    expect(html).toContain('role="tabpanel" id="preview-panel-original" aria-labelledby="preview-tab-original"');
    expect(html).not.toContain("preview-panel-prepared");
  });
});

describe("Tabs のバッジ", () => {
  it("文字列バッジと testId を出し、aria-describedby でタブと結ぶ。タブ単位の aria-label を付けられる", () => {
    const html = renderToStaticMarkup(
      <Tabs
        idPrefix="md"
        value="draft"
        items={[{ id: "draft", label: "下書き", ariaLabel: "下書きの Markdown", badge: "v4", badgeTestId: "draft-meta" }]}
      />
    );
    expect(html).toMatch(/aria-label="下書きの Markdown"[^>]*aria-describedby="md-tab-draft-badge"/);
    expect(html).toMatch(/id="md-tab-draft-badge" data-testid="draft-meta"[^>]*>v4<\/span>/);
  });
});

describe("PageHeader", () => {
  it("utility は secondary と同じ枠を持ち、loading / disabled でもラベルを保つ", () => {
    for (const state of [{}, { loading: true }, { disabled: true }]) {
      const markup = (kind: "utility" | "secondary") => renderToStaticMarkup(
        <PageHeader title="テーブルの管理" actions={[
          { id: "refresh", kind, label: "最新情報を取得", icon: RefreshCw, ...state },
        ]} />
      );
      expect(markup("utility")).toBe(markup("secondary"));
      expect(markup("utility")).toContain("border-border-control");
      expect(markup("utility")).toContain("最新情報を取得");
    }
  });

  it("狭い画面では主操作 1 つだけを見せ、danger はメニューに入れる", () => {
    const a = (id: string, kind: "primary" | "secondary" | "utility" | "danger") => ({ id, kind, label: id });
    const ids = (r: ReturnType<typeof splitCompactActions>) => [r.visible.map((x) => x.id), r.overflow.map((x) => x.id)];
    expect(ids(splitCompactActions([a("del", "danger"), a("new", "primary"), a("reload", "utility"), a("import", "secondary")]))).toEqual([
      ["new"],
      ["import", "reload", "del"],
    ]);
    expect(ids(splitCompactActions([a("del", "danger"), a("del2", "danger")]))).toEqual([[], ["del", "del2"]]);
    expect(ids(splitCompactActions([a("del", "danger")]))).toEqual([["del"], []]);
  });

  it("メニューに入るのが 1 つだけなら畳まず、広い画面と同じ並びで全部出す（#582）", () => {
    const a = (id: string, kind: "primary" | "secondary" | "utility" | "danger") => ({ id, kind, label: id });
    const ids = (r: ReturnType<typeof splitCompactActions>) => [r.visible.map((x) => x.id), r.overflow.map((x) => x.id)];
    // 「一覧へ戻る」+ 保存: 戻るだけのメニューを作らない。
    expect(ids(splitCompactActions([a("back", "secondary"), a("save", "primary")]))).toEqual([["back", "save"], []]);
    expect(ids(splitCompactActions([a("reload", "utility"), a("import", "secondary")]))).toEqual([["reload", "import"], []]);
    expect(ids(splitCompactActions([a("save", "primary"), a("del", "danger")]))).toEqual([["del", "save"], []]);
    // 2 つ以上メニューに入るときは従来どおり畳む。
    expect(ids(splitCompactActions([a("back", "secondary"), a("reload", "utility"), a("save", "primary")]))).toEqual([
      ["save"],
      ["back", "reload"],
    ]);
  });

  it("メニューは ↓ ↑ で循環し、Home / End で端へ移動する", () => {
    expect(nextMenuIndex("ArrowDown", 2, 3)).toBe(0);
    expect(nextMenuIndex("ArrowDown", -1, 3)).toBe(0);
    expect(nextMenuIndex("ArrowUp", 0, 3)).toBe(2);
    expect(nextMenuIndex("Home", 2, 3)).toBe(0);
    expect(nextMenuIndex("End", 0, 3)).toBe(2);
    expect(nextMenuIndex("a", 0, 3)).toBeNull();
    expect(nextMenuIndex("ArrowDown", 0, 0)).toBeNull();
  });

  it("アクションを danger → utility → secondary → primary に並べ、同じ kind は渡した順を保つ", () => {
    const ordered = orderActions([
      { id: "save", kind: "primary", label: "保存" },
      { id: "delete", kind: "danger", label: "削除" },
      { id: "reload", kind: "secondary", label: "再読込" },
      { id: "export", kind: "secondary", label: "CSV" },
      { id: "more", kind: "utility", ariaLabel: "その他" },
    ]);
    expect(ordered.map((action) => action.id)).toEqual(["delete", "more", "reload", "export", "save"]);
  });

  it("PageBody / Section は data-* / aria-* などの属性を DOM に渡す", () => {
    expect(renderToStaticMarkup(<PageBody data-testid="body" aria-label="本文">本文</PageBody>)).toMatch(
      /^<div data-testid="body" aria-label="本文" class=/
    );
    expect(renderToStaticMarkup(<Section id="s" title="見出し" data-testid="section" />)).toMatch(
      /^<section id="s" data-testid="section" class=/
    );
  });

  it("中身は PageBody と同じ計測コンテナに入る（ワイドモニタで左端が揃う）", () => {
    const measure = "max-w-[var(--content-max-width)]";
    expect(renderToStaticMarkup(<PageHeader title="文書" />)).toContain(measure);
    expect(renderToStaticMarkup(<PageBody>本文</PageBody>)).toContain(measure);
    expect(renderToStaticMarkup(<PageHeader title="文書" wide />)).not.toContain(measure);
  });

  it("本文の grid item は内容幅で広がらず、アクション群は折り返す（狭い画面で横にはみ出さない）", () => {
    expect(renderToStaticMarkup(<PageBody>本文</PageBody>)).toContain("[&amp;&gt;*]:min-w-0");
    expect(renderToStaticMarkup(<PageHeader title="文書" actions={[{ id: "a", kind: "primary", label: "保存" }]} />)).toMatch(
      /role="group"[^>]*class="[^"]*flex-wrap/
    );
  });

  it("アクションの testId と ariaLabel、補足の meta を出力する", () => {
    const html = renderToStaticMarkup(
      <PageHeader
        title="文書"
        meta={<span>最終更新 10:00</span>}
        actions={[{ id: "reload", kind: "secondary", label: "再読込", ariaLabel: "文書一覧を再読込", testId: "reload-docs" }]}
      />
    );
    expect(html).toMatch(/aria-label="文書一覧を再読込"[^>]*data-testid="reload-docs"/);
    expect(html).toContain("最終更新 10:00");
    expect(
      renderToStaticMarkup(<PageHeader title="文書" actionsTestId="doc-actions" actions={[{ id: "a", kind: "primary", label: "保存" }]} />)
    ).toMatch(/role="group"[^>]*data-testid="doc-actions"/);
  });

  it("actions が空なら操作の群れを描かない", () => {
    const html = renderToStaticMarkup(<PageHeader title="文書" actionsTestId="doc-actions" actions={[]} />);
    expect(html).not.toContain('data-testid="doc-actions"');
  });
});

describe("Button", () => {
  it("loading 中は先頭アイコンがスピナーに置き換わり、ラベルは変えず aria-busy と disabled が付く", () => {
    const idle = renderToStaticMarkup(<Button icon={Upload}>アップロード</Button>);
    const busy = renderToStaticMarkup(<Button icon={Upload} loading>アップロード</Button>);
    expect(idle).toContain("lucide-upload");
    expect(busy).not.toContain("lucide-upload");
    expect(busy).toContain("animate-spin");
    expect(busy).toContain("アップロード");
    expect(busy).toContain('aria-busy="true"');
    expect(busy).toContain("disabled");
    // 先頭スロットは svg 1 つだけ（スピナー + アイコンの二重表示にしない）
    expect(busy.match(/<svg/g)).toHaveLength(1);
  });

  it("trailingIcon は loading 中に出さない", () => {
    const html = renderToStaticMarkup(<Button trailingIcon={RefreshCw} loading>次へ</Button>);
    expect(html).not.toContain("lucide-refresh-cw");
  });
});

describe("StatusBadge", () => {
  it("既定でバリアントのアイコンを付け、icon={false} で外せる", () => {
    expect(renderToStaticMarkup(<StatusBadge variant="success" label="完了" />)).toContain("lucide-circle-check");
    expect(renderToStaticMarkup(<StatusBadge variant="success" label="完了" icon={false} />)).not.toContain("<svg");
  });

  it("pending は warning と同じ配色（非推奨の別名）", () => {
    const colors = (html: string) => html.match(/border-warning-border bg-warning-subtle text-warning-fg/)?.[0];
    expect(colors(renderToStaticMarkup(<StatusBadge variant="pending" label="待機" />))).toBeTruthy();
    expect(colors(renderToStaticMarkup(<StatusBadge variant="warning" label="注意" />))).toBeTruthy();
  });
});

describe("AppShell", () => {
  it("本文へスキップのリンクと、フォーカスを受けられる <main id=\"pr-main\"> を出力する", () => {
    const html = renderToStaticMarkup(<AppShell sidebar={<nav />}>本文</AppShell>);
    expect(html).toMatch(/<a class="pr-skip-link" href="#pr-main">/);
    expect(html).toMatch(/<main id="pr-main" tabindex="-1"/);
  });
});

describe("TextField", () => {
  it("required は aria-required とバッジだけで伝え、ネイティブの required 検証を付けない", () => {
    const html = renderToStaticMarkup(<TextField id="name" label="名前" required requiredLabel="必須" value="" readOnly />);
    expect(html).toContain('aria-required="true"');
    expect(html).not.toMatch(/<input[^>]*\srequired=""/);
    expect(html).toContain("必須");
  });

  it("必須バッジは状態色を使わず中立色で出し、入力側の aria-required と二重に読み上げない", () => {
    const html = renderToStaticMarkup(<TextField id="name" label="名前" required requiredLabel="必須" value="" readOnly />);
    const badge = html.match(/<span[^>]*>必須<\/span>/)?.[0] ?? "";
    expect(badge).toContain('aria-hidden="true"');
    expect(badge).toContain("text-fg-muted");
    expect(badge).not.toMatch(/warning|danger/);
  });

  it("helper にリンクを含められ、入力欄の aria-describedby と結ばれる", () => {
    const html = renderToStaticMarkup(
      <TextField id="endpoint" label="エンドポイント" value="" readOnly helper={<a href="https://example.com/docs">ドキュメント</a>} />
    );
    const describedBy = html.match(/aria-describedby="([^"]+)"/)?.[1];
    expect(describedBy).toBeTruthy();
    expect(html).toMatch(new RegExp(`<p id="${describedBy}"[^>]*><a href="https://example.com/docs">ドキュメント</a></p>`));
  });
});

describe("RequiredBadge", () => {
  it("既定では読み上げ対象に含め（legend などで単体で使う）、aria-hidden を渡すと外す", () => {
    expect(renderToStaticMarkup(<RequiredBadge label="必須" />)).not.toContain("aria-hidden");
    expect(renderToStaticMarkup(<RequiredBadge label="必須" aria-hidden />)).toContain('aria-hidden="true"');
  });

  it("SelectField も TextField と同じ中立色の必須バッジを出す", () => {
    const html = renderToStaticMarkup(
      <SelectField id="region" label="リージョン" value="a" options={[{ value: "a", label: "A" }]} onValueChange={() => {}} required requiredLabel="必須" />
    );
    const badge = html.match(/<span[^>]*>必須<\/span>/)?.[0] ?? "";
    expect(badge).toContain("text-fg-muted");
    expect(badge).not.toMatch(/warning|danger/);
  });
});

describe("SidebarAccountFooter", () => {
  const password = { id: "password", label: "パスワード変更", icon: KeyRound, onClick: () => {} };

  it("行アクションはナビ行と同じ形で、現在地は aria-current=page", () => {
    const html = renderToStaticMarkup(
      <SidebarAccountFooter name="山田" roles="管理者" collapsed={false} actions={[{ ...password, active: true }]} onLogout={() => {}} />
    );
    expect(html).toContain("山田");
    expect(html).toMatch(/<button type="button" aria-current="page" class="[^"]*bg-accent-emphasis[^"]*">/);
    expect(html).toContain("パスワード変更");
    expect(html).toContain("ログアウト");
  });

  it("名前とロールは 1 行ずつ省略記号で切り、各行の title に全文を出す（括弧で連結しない。#1644）", () => {
    const html = renderToStaticMarkup(
      <SidebarAccountFooter
        name="system_admin"
        roles="システム管理者"
        rolesLabel="ロール: システム管理者"
        collapsed={false}
        onLogout={() => {}}
      />
    );
    // 列は minmax(0, 1fr)。auto の列は nowrap の名前の幅まで広がり、行がサイドバーの外へはみ出す。
    expect(html).toMatch(/^<div class="grid grid-cols-1 gap-1">/);
    expect(html).toMatch(/<div class="[^"]*\bmin-w-0 flex-1\b[^"]*">/);
    expect(html).toMatch(/<div class="[^"]*\btruncate whitespace-nowrap\b[^"]*" title="system_admin">system_admin<\/div>/);
    // 見える行は表示名だけ、title と読み上げは「ロール: …」の全文。
    expect(html).toMatch(
      /<div class="[^"]*\btruncate whitespace-nowrap\b[^"]*" title="ロール: システム管理者"><span class="sr-only">ロール: システム管理者<\/span><span aria-hidden="true">システム管理者<\/span><\/div>/
    );
    expect(html).not.toMatch(/line-clamp/);
  });

  it("account を渡すと欄の行全体がアカウントの画面へのリンクになり、現在地は aria-current=page（#1873）", () => {
    const account = { href: "/rag/account", label: "アカウント設定を開く（山田）" };
    const html = renderToStaticMarkup(<SidebarAccountFooter name="山田" roles="管理者" collapsed={false} account={account} onLogout={() => {}} />);
    expect(html).toMatch(/<a href="\/rag\/account" aria-label="アカウント設定を開く（山田） 山田 管理者" class="[^"]*\bmin-h-11\b[^"]*"/);
    expect(html).not.toContain('aria-current="page"');
    const active = renderToStaticMarkup(
      <SidebarAccountFooter name="山田" roles="管理者" collapsed={false} account={{ ...account, active: true }} />
    );
    expect(active).toMatch(/<a href="\/rag\/account" aria-current="page"[^>]*class="[^"]*bg-accent-emphasis/);
    // 折りたたみ時はアイコンの行のリンク（読み上げ名は同じ）。
    const collapsed = renderToStaticMarkup(<SidebarAccountFooter name="山田" collapsed account={account} />);
    expect(collapsed).toContain('href="/rag/account"');
    expect(collapsed).toContain('aria-label="アカウント設定を開く（山田）"');
  });

  it("rolesLabel を省くとロールの行は roles をそのまま出し、title も roles", () => {
    const html = renderToStaticMarkup(<SidebarAccountFooter name="山田" roles="管理者" collapsed={false} />);
    expect(html).toMatch(/title="管理者">管理者<\/div>/);
    expect(html).not.toContain("sr-only");
  });

  it("折りたたみ時は行アクションとログアウトに読み上げ名を付け、ユーザー名は出さない", () => {
    const html = renderToStaticMarkup(
      <SidebarAccountFooter name="山田" collapsed actions={[password]} onLogout={() => {}} />
    );
    expect(html).not.toContain("山田");
    expect(html).toContain('aria-label="パスワード変更"');
    expect(html).toContain('aria-label="ログアウト"');
  });

  it("notice はログアウトの代わりに置け、name を省くとユーザー領域を出さない", () => {
    const html = renderToStaticMarkup(
      <SidebarAccountFooter collapsed={false} notice={<p role="status">ローカル DEBUG</p>} />
    );
    expect(html).toContain('<p role="status">ローカル DEBUG</p>');
    expect(html).not.toContain("ログアウト");
    expect(html).not.toMatch(/lucide-user-round/);
  });

  it("テーマ切替を出さない（#17。配色はライトの 1 種類だけ）", () => {
    const html = renderToStaticMarkup(<SidebarAccountFooter collapsed={false} onLogout={() => {}} />);
    expect(html).toContain("ログアウト");
    expect(html).not.toMatch(/テーマに切り替え|lucide-moon|lucide-sun/);
  });
});
