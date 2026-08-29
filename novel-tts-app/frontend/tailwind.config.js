/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ['./src/**/*.{ts,tsx,js,jsx,mdx}'],
  darkMode: 'class',
  theme: {
    extend: {
      // 颜色全部改为 CSS 变量驱动，由 globals.css 中的 :root / html.light 分别赋值
      // 这样组件里的 bg-ink-50、text-ink-700、bg-white/10 在浅色/深色下自动切换
      colors: {
        white: 'rgba(var(--color-white), <alpha-value>)',
        black: 'rgba(var(--color-black), <alpha-value>)',
        ink: {
          0:   'rgba(var(--ink-0), <alpha-value>)',
          50:  'rgba(var(--ink-50), <alpha-value>)',
          100: 'rgba(var(--ink-100), <alpha-value>)',
          200: 'rgba(var(--ink-200), <alpha-value>)',
          300: 'rgba(var(--ink-300), <alpha-value>)',
          400: 'rgba(var(--ink-400), <alpha-value>)',
          500: 'rgba(var(--ink-500), <alpha-value>)',
          600: 'rgba(var(--ink-600), <alpha-value>)',
          700: 'rgba(var(--ink-700), <alpha-value>)',
          800: 'rgba(var(--ink-800), <alpha-value>)',
          900: 'rgba(var(--ink-900), <alpha-value>)',
        },
        cream: {
          50:  'rgba(var(--cream-50), <alpha-value>)',
          100: 'rgba(var(--cream-100), <alpha-value>)',
          200: 'rgba(var(--cream-200), <alpha-value>)',
          300: 'rgba(var(--cream-300), <alpha-value>)',
        },
        // 品牌紫（深浅模式保持一致）— 使用全局的 --brand-* 三通道变量，
        // 使组件里写 text-brand-500/70、bg-brand-100/40 等 opacity modifier 都能工作。
        brand: {
          DEFAULT: 'rgba(var(--brand-500), <alpha-value>)',
          50:  'rgba(var(--brand-50), <alpha-value>)',
          100: 'rgba(var(--brand-100), <alpha-value>)',
          200: 'rgba(var(--brand-200), <alpha-value>)',
          300: 'rgba(var(--brand-300), <alpha-value>)',
          400: 'rgba(var(--brand-400), <alpha-value>)',
          500: 'rgba(var(--brand-500), <alpha-value>)',
          600: 'rgba(var(--brand-600), <alpha-value>)',
          700: 'rgba(var(--brand-700), <alpha-value>)',
          800: 'rgba(var(--brand-800), <alpha-value>)',
          900: 'rgba(var(--brand-900), <alpha-value>)',
        },
        accent: {
          lime:  'rgba(var(--accent-lime), <alpha-value>)',
          amber: 'rgba(var(--accent-amber), <alpha-value>)',
          teal:  'rgba(var(--accent-teal), <alpha-value>)',
          rose:  'rgba(var(--accent-rose), <alpha-value>)',
        },
        // status 语义色 — 深色/浅色模式下自动切换对比度
        status: {
          info:  'rgba(var(--status-info-bg), <alpha-value>)',
          warn:  'rgba(var(--status-warn-bg), <alpha-value>)',
          synth: 'rgba(var(--status-synth-bg), <alpha-value>)',
          success: 'rgba(var(--status-success-bg), <alpha-value>)',
          ready: 'rgba(var(--status-ready-bg), <alpha-value>)',
          partial: 'rgba(var(--status-partial-bg), <alpha-value>)',
          error: 'rgba(var(--status-error-bg), <alpha-value>)',
          muted: 'rgba(var(--status-muted-bg), <alpha-value>)',
        },
      },
      fontFamily: {
        sans: [
          'Geist',
          'ui-sans-serif',
          'system-ui',
          '-apple-system',
          'Segoe UI',
          'PingFang SC',
          'Microsoft YaHei',
          'sans-serif',
        ],
        // Display/大标题：Sora — 几何锐度、强张力、AI引擎感
        display: [
          'Sora',
          'Geist',
          'PingFang SC',
          'ui-sans-serif',
          'sans-serif',
        ],
        serif: [
          'Sora',
          'PingFang SC',
          'ui-sans-serif',
          'sans-serif',
        ],
        mono: [
          'Geist Mono',
          'ui-monospace',
          'SF Mono',
          'Menlo',
          'monospace',
        ],
      },
      boxShadow: {
        'el-sm': '0 1px 0 0 rgb(var(--color-white) / 0.04) inset, 0 1px 2px 0 rgba(0,0,0,0.4)',
        'el': '0 0 0 1px rgb(var(--color-white) / 0.04), 0 8px 24px -6px rgba(0,0,0,0.45), 0 2px 4px -2px rgba(0,0,0,0.3)',
        'el-lg': '0 0 0 1px rgb(var(--color-white) / 0.06), 0 24px 48px -12px rgba(0,0,0,0.55), 0 4px 8px -4px rgba(0,0,0,0.3)',
        'el-xl': '0 0 0 1px rgb(var(--color-white) / 0.06), 0 40px 80px -20px rgba(0,0,0,0.6), 0 8px 16px -8px rgba(0,0,0,0.35)',
        'brand': '0 0 0 1px rgba(168,85,247,0.4), 0 10px 30px -8px rgba(139,92,246,0.5)',
        'brand-lg': '0 0 0 1px rgba(168,85,247,0.5), 0 16px 40px -10px rgba(139,92,246,0.55)',
      },
      borderRadius: {
        '2xl': '1rem',
        '3xl': '1.25rem',
        '4xl': '1.5rem',
      },
      backgroundImage: {
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
