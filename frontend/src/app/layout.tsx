import type { Metadata } from 'next';
import './globals.css';
import { ThemeProvider } from '@/components/ThemeContext';
import { AuthProvider } from '@/components/AuthContext';

export const metadata: Metadata = {
  title: '阿布 · AI 内容创作平台',
  description: 'AI 驱动的一站式内容创作平台 —— 小说、有声书、短剧、漫画，全链路创作工作流',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN" className="dark">
      <body className="min-h-screen">
        <ThemeProvider>
          <AuthProvider>{children}</AuthProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
