// @vitest-environment happy-dom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  AUTH_BROADCAST_CHANNEL,
  AuthProvider,
  RequireAuth,
  useAuth,
  type AuthApi,
  type AuthContextValue,
  type BaseCurrentUser,
} from "../src";

// #1565: ログインの Cookie は 3 製品で共有する。ある製品のログアウト・ログインを、
// 開いているほかの製品・タブへ BroadcastChannel で知らせる。

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const routes = { login: "/login", passwordChange: "/password/change", forbidden: "/forbidden" };

function user(overrides: Partial<BaseCurrentUser> = {}): BaseCurrentUser {
  return {
    user_uuid: "user-1",
    login_user_id: "sales.user",
    display_name: "営業ユーザー",
    status: "ACTIVE",
    force_password_change: false,
    role_codes: ["SALES"],
    is_system_admin: false,
    permissions: ["menu.search"],
    debug_mode: false,
    password_change_allowed: true,
    ...overrides,
  };
}

class HttpError extends Error {
  constructor(readonly status: number) {
    super(`HTTP ${status}`);
  }
}

/** 同じ名前のほかの channel にだけ届ける（送った channel 自身には届かない。ブラウザと同じ）。 */
class FakeBroadcastChannel extends EventTarget {
  static instances: FakeBroadcastChannel[] = [];
  closed = false;

  constructor(readonly name: string) {
    super();
    FakeBroadcastChannel.instances.push(this);
  }

  postMessage(data: unknown): void {
    if (this.closed) throw new Error("closed");
    for (const other of FakeBroadcastChannel.instances) {
      if (other !== this && !other.closed && other.name === this.name) {
        other.dispatchEvent(new MessageEvent("message", { data }));
      }
    }
  }

  close(): void {
    this.closed = true;
  }
}

/** 共有の Cookie のセッション（3 製品の backend が同じ表を読む）を真似る。 */
function sharedSession(initial: BaseCurrentUser | null) {
  let current = initial;
  const api = (): AuthApi => ({
    me: () => (current ? Promise.resolve(current) : Promise.reject(new HttpError(401))),
    login: () => {
      current = user({ user_uuid: "user-2", display_name: "別の利用者" });
      return Promise.resolve(current);
    },
    logout: () => {
      current = null;
      return Promise.resolve();
    },
    changePassword: () => {
      current = null;
      return Promise.resolve({ changed: true });
    },
  });
  return { api };
}

interface Mounted {
  host: HTMLDivElement;
  root: Root;
  auth: () => AuthContextValue;
}

const mounted: Mounted[] = [];

async function mount(api: AuthApi): Promise<Mounted> {
  const host = document.createElement("div");
  document.body.append(host);
  const root = createRoot(host);
  let latest: AuthContextValue | null = null;
  function Capture() {
    latest = useAuth();
    return null;
  }
  await act(async () => {
    root.render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter initialEntries={["/work"]}>
          <AuthProvider api={api}>
            <Capture />
            <Routes>
              <Route path="/login" element={<p>ログイン画面</p>} />
              <Route
                path="/work"
                element={
                  <RequireAuth routes={routes}>
                    <p>{"作業の画面"}</p>
                  </RequireAuth>
                }
              />
            </Routes>
          </AuthProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    );
  });
  await act(async () => undefined);
  const item: Mounted = {
    host,
    root,
    auth: () => {
      if (!latest) throw new Error("AuthProvider が描画されていません。");
      return latest;
    },
  };
  mounted.push(item);
  return item;
}

beforeEach(() => {
  FakeBroadcastChannel.instances = [];
  vi.stubGlobal("BroadcastChannel", FakeBroadcastChannel);
});

afterEach(() => {
  for (const item of mounted.splice(0)) {
    act(() => item.root.unmount());
    item.host.remove();
  }
  vi.unstubAllGlobals();
});

describe("製品・タブの間のログイン状態の通知（#1565）", () => {
  it("ある製品でログアウトすると、ほかの製品の画面もログイン画面へ移る", async () => {
    const session = sharedSession(user());
    const rag = await mount(session.api());
    const nl2sql = await mount(session.api());
    expect(rag.host.textContent).toContain("作業の画面");
    expect(nl2sql.host.textContent).toContain("作業の画面");
    expect(FakeBroadcastChannel.instances.map((channel) => channel.name)).toEqual([
      AUTH_BROADCAST_CHANNEL,
      AUTH_BROADCAST_CHANNEL,
    ]);

    await act(async () => {
      await rag.auth().logout();
    });
    await act(async () => undefined);

    expect(rag.host.textContent).toContain("ログイン画面");
    expect(nl2sql.host.textContent).toContain("ログイン画面");
    expect(nl2sql.auth().status).toBe("unauthenticated");
  });

  it("ある製品でログインし直すと、ほかの製品は共有の Cookie で利用者を確かめ直す", async () => {
    const session = sharedSession(null);
    const rag = await mount(session.api());
    const agent = await mount(session.api());
    expect(agent.host.textContent).toContain("ログイン画面");

    await act(async () => {
      await rag.auth().login("other.user", "password");
    });
    await act(async () => undefined);

    expect(agent.auth().status).toBe("authenticated");
    expect(agent.auth().user?.user_uuid).toBe("user-2");
  });

  it("パスワードを変えると、ほかの製品の画面も未認証に戻る", async () => {
    const session = sharedSession(user());
    const rag = await mount(session.api());
    const nl2sql = await mount(session.api());

    await act(async () => {
      await rag.auth().changePassword("old", "new");
    });
    await act(async () => undefined);

    expect(nl2sql.host.textContent).toContain("ログイン画面");
  });

  it("ログアウトの API が失敗したら、ほかの製品へは知らせない（セッションは残っている）", async () => {
    const session = sharedSession(user());
    const failing: AuthApi = { ...session.api(), logout: () => Promise.reject(new HttpError(503)) };
    const rag = await mount(failing);
    const nl2sql = await mount(session.api());

    await act(async () => {
      await rag.auth().logout().catch(() => undefined);
    });
    await act(async () => undefined);

    expect(rag.host.textContent).toContain("ログイン画面");
    expect(nl2sql.host.textContent).toContain("作業の画面");
  });

  it("知らない形のメッセージは無視する", async () => {
    const session = sharedSession(user());
    const nl2sql = await mount(session.api());
    const outsider = new FakeBroadcastChannel(AUTH_BROADCAST_CHANNEL);

    await act(async () => {
      outsider.postMessage("logout");
      outsider.postMessage({ type: "other" });
    });

    expect(nl2sql.host.textContent).toContain("作業の画面");
  });

  it("BroadcastChannel の無い環境でも、ログイン・ログアウトは動く", async () => {
    vi.stubGlobal("BroadcastChannel", undefined);
    const session = sharedSession(user());
    const rag = await mount(session.api());
    expect(rag.host.textContent).toContain("作業の画面");

    await act(async () => {
      await rag.auth().logout();
    });
    await act(async () => undefined);
    expect(rag.host.textContent).toContain("ログイン画面");

    await act(async () => {
      await rag.auth().login("other.user", "password");
    });
    expect(rag.auth().status).toBe("authenticated");
  });
});
