// 会话竞态测试：退出后的旧请求绝不能恢复身份、修改口令或重新刷新领域数据。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("element-plus", () => ({
  ElMessage: { error: vi.fn(), warning: vi.fn() },
}));
vi.mock("../../shared/confirm", () => ({ confirmAction: vi.fn() }));
vi.mock("../../shared/api", () => ({
  authApi: {
    login: vi.fn(),
    me: vi.fn(),
    logout: vi.fn(),
    password: vi.fn(),
  },
  clearToken: vi.fn(),
}));

import { confirmAction } from "../../shared/confirm";
import { authApi, clearToken, type SessionUser } from "../../shared/api";
import { usePlatformSession } from "./usePlatformSession";

const account: SessionUser = {
  id: "account",
  username: "account",
  displayName: "测试账号",
  isAdmin: false,
  scopes: ["tasks:read"],
  enabled: true,
  mustChangePassword: false,
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function createSession(loadInitial = vi.fn(async () => undefined)) {
  const clearWorkspace = vi.fn();
  const refreshWorkspace = vi.fn(async () => undefined);
  return {
    session: usePlatformSession({
      loadInitial,
      clearWorkspace,
      refreshWorkspace,
    }),
    loadInitial,
    clearWorkspace,
    refreshWorkspace,
  };
}

beforeEach(() => {
  vi.stubGlobal("window", new EventTarget());
  vi.mocked(authApi.logout).mockResolvedValue(undefined);
  vi.mocked(confirmAction).mockResolvedValue(true);
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("usePlatformSession", () => {
  it("退出后忽略恢复流程中迟到的初始加载", async () => {
    const initial = deferred<void>();
    const { session, loadInitial, clearWorkspace } = createSession(
      vi.fn(() => initial.promise),
    );
    vi.mocked(authApi.me).mockResolvedValue({ user: account });

    session.start();
    await vi.waitFor(() => expect(loadInitial).toHaveBeenCalledOnce());
    await session.logout();
    initial.resolve();
    await vi.waitFor(() => expect(session.user.value).toBeUndefined());

    expect(session.authenticated.value).toBe(false);
    expect(session.passwordOpen.value).toBe(false);
    expect(clearWorkspace).toHaveBeenCalledOnce();
    session.stop();
  });

  it("恢复后的领域加载失败会清除半完成身份", async () => {
    const { session, clearWorkspace } = createSession(
      vi.fn(async () => {
        throw new Error("初始加载失败");
      }),
    );
    vi.mocked(authApi.me).mockResolvedValue({ user: account });

    session.start();
    await vi.waitFor(() => expect(clearWorkspace).toHaveBeenCalledOnce());

    expect(session.user.value).toBeUndefined();
    expect(session.authenticated.value).toBe(false);
    expect(session.passwordOpen.value).toBe(false);
    expect(clearWorkspace).toHaveBeenCalledOnce();
    expect(clearToken).toHaveBeenCalledOnce();
    session.stop();
  });

  it("退出后忽略迟到的改密响应和刷新回调", async () => {
    const passwordRequest = deferred<{ user: SessionUser }>();
    const { session, refreshWorkspace } = createSession();
    vi.mocked(authApi.login).mockResolvedValue({ user: account });
    vi.mocked(authApi.password).mockReturnValue(passwordRequest.promise);
    await session.login(account.username, "valid-password");

    const changing = session.changePassword("valid-password", "new-password-123");
    await vi.waitFor(() => expect(authApi.password).toHaveBeenCalledOnce());
    await session.logout();
    passwordRequest.resolve({ user: { ...account, displayName: "新名称" } });

    await expect(changing).resolves.toBe(false);
    expect(session.user.value).toBeUndefined();
    expect(session.passwordOpen.value).toBe(false);
    expect(session.passwordSaving.value).toBe(false);
    expect(refreshWorkspace).not.toHaveBeenCalled();
  });
});
