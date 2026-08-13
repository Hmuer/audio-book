import type { Metadata } from 'next';
import './globals.css';
import { ThemeProvider } from '@/components/ThemeContext';
import { AuthProvider } from '@/components/AuthContext';

export const metadata: Metadata = {
  title: 'AI 有声小说生成器',
  description: '输入小说文本 → LLM 识别角色/对白 → 多音色合成 MP3',
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
