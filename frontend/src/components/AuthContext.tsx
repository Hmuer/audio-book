'use client';

import { createContext, useContext, useEffect, useState, useCallback, ReactNode } from 'react';
import { api, getToken, setToken, clearToken, isLoggedIn, setOnAuthFail, UserInfo } from '@/lib/api';

interface AuthState {
  user: UserInfo | null;
  loading: boolean; // 初始化时校验 token，期间 loading=true
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  refresh: () => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<UserInfo | null>(null);
  const [loading, setLoading] = useState(true);

  // 初始化：若 localStorage 有 token → 调 /auth/me 校验有效性
  const refresh = useCallback(async () => {
    if (!isLoggedIn()) {
      setUser(null);
      setLoading(false);
      return;
    }
    try {
      const me = await api.authMe();
      setUser(me);
    } catch {
      // token 无效，clearToken 已经在 _fetch 401 里做了
      setUser(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    // 注册 401 回调：任意请求 401 → 强制刷新登录态
    setOnAuthFail(() => {
      setUser(null);
      // 不主动改 hash，让上层 page.tsx 路由层感知 user===null 后跳转
      if (typeof window !== 'undefined' && window.location.hash !== '#/login') {
        window.location.hash = '/login';
      }
    });
    refresh();
  }, [refresh]);

  const login = useCallback(async (username: string, password: string) => {
    const resp = await api.authLogin(username, password);
    setToken(resp.token, resp.expires_at);
    setUser(resp.user);
  }, []);

  const logout = useCallback(async () => {
    try {
      await api.authLogout();
    } catch {
      // 后端 logout 失败也无所谓，前端清 token 即可
    }
    clearToken();
    setUser(null);
    if (typeof window !== 'undefined') {
      window.location.hash = '/login';
    }
  }, []);

  return (
    <AuthContext.Provider value={{ user, loading, login, logout, refresh }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error('useAuth must be used within AuthProvider');
  }
  return ctx;
}
