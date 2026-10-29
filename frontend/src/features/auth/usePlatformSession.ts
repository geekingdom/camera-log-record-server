// 平台会话状态集中处理登录、恢复、改密和失效事件；领域数据刷新仍由装配层显式注入。
import { ref } from "vue";
import { ElMessage } from "element-plus";
import { confirmAction } from "../../shared/confirm";
import {
  authApi,
  clearToken,
  type SessionUser,
} from "../../shared/api";

interface SessionOptions {
  /** 登录或恢复成功后由根协调层加载该账号可见的领域数据。 */
  loadInitial(user: SessionUser, generation: number): Promise<void>;
  /** 本地会话失效时先撤销页面编辑态和异步请求，再向服务端注销。 */
  clearWorkspace(): void;
  /** 用户主动改密完成后刷新当前工作区。 */
  refreshWorkspace(): Promise<void>;
}

/** 会话代次使旧登录、恢复和领域加载回调无法覆盖后来的登录状态。 */
export function usePlatformSession(options: SessionOptions) {
  const authenticated = ref(false);
  const busy = ref(false);
  const user = ref<SessionUser>();
  const passwordOpen = ref(false);
  const passwordSaving = ref(false);
  const generation = ref(0);
  let identityRefreshTimer: ReturnType<typeof setTimeout> | undefined;
  let identityRefreshing = false;
  let monitoringIdentity = false;

  const reportError = (value: unknown) =>
    ElMessage.error(value instanceof Error ? value.message : "请求失败");

  /** 清除本地身份和领域视图；调用方负责先确认自己仍是当前会话代次。 */
  function clearLocalSession() {
    authenticated.value = false;
    user.value = undefined;
    passwordOpen.value = false;
    options.clearWorkspace();
  }

  /** 按上海自然日计算下一个零点，权限截止日只精确到天，不能依赖浏览器所在时区。 */
  function millisecondsUntilShanghaiMidnight(now = new Date()) {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: "Asia/Shanghai", year: "numeric", month: "numeric", day: "numeric",
    }).formatToParts(now);
    const value = (type: string) => Number(parts.find((part) => part.type === type)?.value);
    const nextMidnight = Date.UTC(value("year"), value("month") - 1, value("day") + 1) - 8 * 60 * 60 * 1000;
    return Math.max(1_000, nextMidnight - now.getTime());
  }

  /** 只刷新身份快照；导航会响应 isAdmin 与 scope 的变化，具体资源操作仍由服务端再次鉴权。 */
  async function refreshIdentity() {
    if (!authenticated.value || identityRefreshing) return;
    const current = generation.value;
    identityRefreshing = true;
    try {
      const refreshed = await authApi.me();
      if (current !== generation.value) return;
      user.value = refreshed.user;
      passwordOpen.value = refreshed.user.mustChangePassword;
    } catch {
      // request() 在认证失效时已派发 auth-required；这里避免将网络瞬态误当作本地退出。
    } finally {
      identityRefreshing = false;
    }
  }

  function scheduleIdentityRefresh() {
    if (identityRefreshTimer) clearTimeout(identityRefreshTimer);
    if (!monitoringIdentity || !authenticated.value) return;
    identityRefreshTimer = setTimeout(() => {
      void refreshIdentity().finally(scheduleIdentityRefresh);
    }, millisecondsUntilShanghaiMidnight());
  }

  function refreshIdentityWhenVisible() {
    if (document.visibilityState === "visible") void refreshIdentity();
  }

  async function establish(sessionUser: SessionUser, current: number) {
    if (current !== generation.value) return false;
    user.value = sessionUser;
    passwordOpen.value = sessionUser.mustChangePassword;
    if (!sessionUser.mustChangePassword)
      await options.loadInitial(sessionUser, current);
    if (current !== generation.value) return false;
    authenticated.value = true;
    scheduleIdentityRefresh();
    return true;
  }

  /** 登录成功才返回 true，供输入组件在成功后清除密码框。 */
  async function login(username: string, password: string) {
    if (!username.trim() || !password) {
      ElMessage.warning("请输入用户名和密码");
      return false;
    }
    busy.value = true;
    const current = ++generation.value;
    try {
      const loggedIn = await authApi.login(username.trim(), password);
      return await establish(loggedIn.user, current);
    } catch (value) {
      if (current === generation.value) {
        clearLocalSession();
        reportError(value);
      }
      return false;
    } finally {
      if (current === generation.value) busy.value = false;
    }
  }

  async function restore() {
    const current = ++generation.value;
    try {
      const restored = await authApi.me();
      await establish(restored.user, current);
    } catch {
      if (current === generation.value) {
        clearToken();
        clearLocalSession();
      }
    }
  }

  /** 本地状态优先失效，确保退出后的延迟响应不能重新打开编辑器或污染列表。 */
  async function logout() {
    const current = ++generation.value;
    busy.value = false;
    passwordSaving.value = false;
    clearToken();
    clearLocalSession();
    try {
      await authApi.logout();
    } catch {
      /* 会话已失效时仍完成本地退出。 */
    }
    if (current === generation.value) clearToken();
  }

  /** 强制改密与手动改密共用相同校验和确认语义。 */
  async function changePassword(currentPassword: string, nextPassword: string) {
    if (passwordSaving.value) return false;
    const current = generation.value;
    if (nextPassword.length < 8 || nextPassword.length > 128) {
      ElMessage.warning("新密码长度须为 8 至 128 位");
      return false;
    }
    if (
      !(await confirmAction(
        "确认修改当前账号密码并使其他登录会话失效？",
        "确认修改密码",
      ))
    )
      return false;
    if (current !== generation.value) return false;
    passwordSaving.value = true;
    try {
      const changedUser = (
        await authApi.password(currentPassword, nextPassword)
      ).user;
      if (current !== generation.value) return false;
      user.value = changedUser;
      passwordOpen.value = false;
      await options.refreshWorkspace();
      return current === generation.value;
    } catch (value) {
      if (current === generation.value) reportError(value);
      return false;
    } finally {
      if (current === generation.value) passwordSaving.value = false;
    }
  }

  function start() {
    monitoringIdentity = true;
    void restore();
    window.addEventListener("auth-required", logout);
    document.addEventListener("visibilitychange", refreshIdentityWhenVisible);
  }
  function stop() {
    monitoringIdentity = false;
    if (identityRefreshTimer) clearTimeout(identityRefreshTimer);
    identityRefreshTimer = undefined;
    window.removeEventListener("auth-required", logout);
    document.removeEventListener("visibilitychange", refreshIdentityWhenVisible);
  }

  return {
    authenticated,
    busy,
    user,
    passwordOpen,
    passwordSaving,
    generation,
    login,
    logout,
    changePassword,
    start,
    stop,
  };
}
