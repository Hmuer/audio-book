/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ['./src/**/*.{ts,tsx,js,jsx,mdx}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        // ElevenLabs 风格：暖奶油（不是冰冷的灰黑）底色
        // 品牌色：深青调（Eleven 主色是 #16161a + 品牌黑 #0f0f12 + 强调色 #6366f1 变体）
        ink: {
          0: '#09090b',
          50: '#0f0f12',   // 主底色（Eleven 那种极深炭黑）
          100: '#15151a',  // 卡片底
          200: '#1c1c24',  // 卡片底 2
          300: '#262631',  // 分割
          400: '#3a3a47',  // 边框 hover
          500: '#555562',  // 边框
          600: '#8b8b97',  // 次级文本
          700: '#c5c5cc',  // 正文
          800: '#e8e8eb',  // 强文本
          900: '#ffffff',  // 纯白（仅高对比）
        },
        brand: {
          50: '#f5f3ff',
          100: '#ede9fe',
          200: '#ddd6fe',
          300: '#c4b5fd',
          400: '#a78bfa',
          500: '#8b5cf6',  // Eleven 紫
          600: '#7c3aed',
          700: '#6d28d9',
          800: '#5b21b6',
          900: '#4c1d95',
        },
        cream: {
          50: '#fbfaf6',
          100: '#f5f2ea',
          200: '#e8e2d2',
          300: '#cfc6ad',
        },
        accent: {
          lime: '#c6f44a',   // ElevenLabs 标志性青柠点缀
          amber: '#f59e0b',
          teal: '#2dd4bf',
          rose: '#fb7185',
        },
      },
      fontFamily: {
        sans: [
          'Inter',
          'ui-sans-serif',
          'system-ui',
          '-apple-system',
          'Segoe UI',
          'PingFang SC',
          'Microsoft YaHei',
          'sans-serif',
        ],
        display: [
          'Space Grotesk',
          'Inter',
          'PingFang SC',
          'ui-sans-serif',
          'sans-serif',
        ],
        mono: [
          'JetBrains Mono',
          'ui-monospace',
          'SF Mono',
          'Menlo',
          'monospace',
        ],
      },
      boxShadow: {
        // ElevenLabs 的克制阴影：不是长投影，而是柔和的双层
        'el-sm': '0 1px 0 0 rgba(255,255,255,0.04) inset, 0 1px 2px 0 rgba(0,0,0,0.4)',
        'el': '0 0 0 1px rgba(255,255,255,0.04), 0 8px 24px -6px rgba(0,0,0,0.45), 0 2px 4px -2px rgba(0,0,0,0.3)',
        'el-lg': '0 0 0 1px rgba(255,255,255,0.06), 0 24px 48px -12px rgba(0,0,0,0.55), 0 4px 8px -4px rgba(0,0,0,0.3)',
        'el-xl': '0 0 0 1px rgba(255,255,255,0.06), 0 40px 80px -20px rgba(0,0,0,0.6), 0 8px 16px -8px rgba(0,0,0,0.35)',
        // 主 CTA：品牌色外发光
        'brand': '0 0 0 1px rgba(168,85,247,0.4), 0 10px 30px -8px rgba(139,92,246,0.5)',
        'brand-lg': '0 0 0 1px rgba(168,85,247,0.5), 0 16px 40px -10px rgba(139,92,246,0.55)',
      },
      borderRadius: {
        // Eleven 风格：按钮/输入 10px，卡片 16-20px，面板 24px
        '2xl': '1rem',      // 16px 卡片
        '3xl': '1.25rem',   // 20px 面板
        '4xl': '1.5rem',    // 24px 主面板
      },
      backgroundImage: {
        // ElevenLabs hero 常用：左上品牌紫 + 右下淡青 + 噪点
        'hero-grad': 'radial-gradient(1200px 600px at 0% 0%, rgba(139,92,246,0.18), transparent 60%), radial-gradient(800px 500px at 100% 10%, rgba(45,212,191,0.08), transparent 60%), radial-gradient(900px 600px at 50% 120%, rgba(198,244,74,0.06), transparent 60%)',
      },
      keyframes: {
        'pulse-soft': {
          '0%, 100%': { opacity: 1 },
          '50%': { opacity: 0.55 },
        },
        'scale-in': {
          '0%': { transform: 'scale(0.97)', opacity: 0 },
          '100%': { transform: 'scale(1)', opacity: 1 },
        },
        'shimmer': {
          '0%': { backgroundPosition: '-200% 0' },
          '100%': { backgroundPosition: '200% 0' },
        },
      },
      animation: {
        'pulse-soft': 'pulse-soft 2.2s ease-in-out infinite',
        'scale-in': 'scale-in 0.22s cubic-bezier(0.2, 0.9, 0.3, 1.0)',
        'shimmer': 'shimmer 2.2s linear infinite',
      },
    },
  },
  plugins: [],
};
