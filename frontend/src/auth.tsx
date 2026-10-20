import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode
} from "react";
import { useQueryClient } from "@tanstack/react-query";
import { invalidateAuthenticationRequests, isSessionInvalidationError, setUnauthorizedHandler } from "./client";
import type { Session } from "./security";
import { authApi } from "./securityApi";

type AuthStatus = "booting" | "unavailable" | "anonymous" | "authenticated";
type AuthContextValue = {
  status: AuthStatus;
  session: Session | null;
  bootError: string;
  logoutError: string;
  logoutPending: boolean;
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  changePassword: (currentPassword: string, newPassword: string) => Promise<void>;
  refetchMe: () => Promise<void>;
};

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [status, setStatus] = useState<AuthStatus>("booting");
  const [session, setSession] = useState<Session | null>(null);
  const [bootError, setBootError] = useState("");
  const [logoutError, setLogoutError] = useState("");
  const [logoutPending, setLogoutPending] = useState(false);
  const epoch = useRef(0);
  const mounted = useRef(false);
  const verifiedSession = useRef<Session | null>(null);
  const pendingMe = useRef<AbortController | null>(null);
  const mutation = useRef<"login" | "logout" | "password" | null>(null);

  const advanceEpoch = useCallback(() => {
    epoch.current += 1;
    invalidateAuthenticationRequests();
    pendingMe.current?.abort();
    pendingMe.current = null;
    return epoch.current;
  }, []);

  const clearSession = useCallback(() => {
    advanceEpoch();
    queryClient.clear();
    verifiedSession.current = null;
    setSession(null);
    setBootError("");
    setLogoutError("");
    setStatus("anonymous");
  }, [advanceEpoch, queryClient]);

  const loadMe = useCallback(async (operation: number) => {
    pendingMe.current?.abort();
    const controller = new AbortController();
    pendingMe.current = controller;
    const current = () => mounted.current && epoch.current === operation && pendingMe.current === controller;
    setStatus((previous) => previous === "authenticated" ? previous : "booting");
    try {
      const next = await authApi.me(controller.signal);
      if (!current()) return;
      if (verifiedSession.current?.user.id !== next.user.id) {
        advanceEpoch();
        queryClient.clear();
      }
      verifiedSession.current = next;
      setSession(next);
      setBootError("");
      setStatus("authenticated");
    } catch (error: unknown) {
      if (!current()) return;
      if (isSessionInvalidationError(error)) {
        clearSession();
      } else {
        // A failed verification is not evidence that the server session expired.
        setBootError(error instanceof Error ? error.message : "无法连接认证服务。");
        setStatus("unavailable");
      }
    } finally {
      if (pendingMe.current === controller) pendingMe.current = null;
    }
  }, [advanceEpoch, clearSession, queryClient]);

  const refetchMe = useCallback(async () => {
    if (mutation.current) return;
    await loadMe(epoch.current);
  }, [loadMe]);

  useEffect(() => {
    mounted.current = true;
    const operation = advanceEpoch();
    const removeHandler = setUnauthorizedHandler(clearSession);
    void loadMe(operation);
    return () => {
      mounted.current = false;
      advanceEpoch();
      removeHandler();
    };
  }, [advanceEpoch, clearSession, loadMe]);

  const login = useCallback(async (username: string, password: string) => {
    if (mutation.current) throw new Error("正在处理认证请求，请稍后重试。");
    mutation.current = "login";
    const operation = advanceEpoch();
    queryClient.clear();
    verifiedSession.current = null;
    setSession(null);
    setBootError("");
    setLogoutError("");
    setStatus("anonymous");
    try {
      await authApi.login(username, password);
      if (mounted.current && epoch.current === operation) await loadMe(operation);
    } finally {
      mutation.current = null;
    }
  }, [advanceEpoch, loadMe, queryClient]);

  const logout = useCallback(async () => {
    if (mutation.current) {
      if (mutation.current !== "logout") setLogoutError("退出未完成：正在处理认证请求，请稍后重试退出。");
      return;
    }
    mutation.current = "logout";
    const operation = advanceEpoch();
    setLogoutPending(true);
    setLogoutError("");
    try {
      await authApi.logout();
      if (mounted.current && epoch.current === operation) clearSession();
    } catch (error: unknown) {
      if (!mounted.current || epoch.current !== operation) return;
      if (isSessionInvalidationError(error)) clearSession();
      else setLogoutError(`退出未完成：${error instanceof Error ? error.message : "无法连接认证服务。"} 请重试退出。`);
    } finally {
      mutation.current = null;
      if (mounted.current) setLogoutPending(false);
    }
  }, [advanceEpoch, clearSession]);

  const changePassword = useCallback(async (currentPassword: string, newPassword: string) => {
    if (mutation.current) throw new Error("正在处理认证请求，请稍后重试。");
    mutation.current = "password";
    const operation = epoch.current;
    try {
      await authApi.changePassword(currentPassword, newPassword);
      if (mounted.current && epoch.current === operation) await loadMe(operation);
    } finally {
      mutation.current = null;
    }
  }, [loadMe]);

  const value = useMemo(() => ({
    status,
    session,
    bootError,
    logoutError,
    logoutPending,
    login,
    logout,
    changePassword,
    refetchMe
  }), [bootError, changePassword, login, logout, logoutError, logoutPending, refetchMe, session, status]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside AuthProvider");
  return value;
}
