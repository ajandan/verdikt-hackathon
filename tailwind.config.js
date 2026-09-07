/** @type {import('tailwindcss').Config} */
module.exports = {
  darkMode: 'class',
  content: [
    './src/docgen/ui/templates/**/*.html',
  ],
  theme: {
    extend: {
      fontFamily: {
        sans: ['Inter', 'ui-sans-serif', 'system-ui', 'sans-serif'],
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'Consolas', 'monospace'],
      },
      colors: {
        // Bridge to CSS tokens so hand-written class="bg-brand" etc. picks up dark-mode too.
        brand: {
          DEFAULT: 'rgb(var(--brand-rgb) / <alpha-value>)',
          hover: 'rgb(var(--brand-hover-rgb) / <alpha-value>)',
          soft: 'rgb(var(--brand-rgb) / 0.1)',
          softer: 'rgb(var(--brand-rgb) / 0.05)',
        },
        accent: {
          DEFAULT: 'rgb(var(--accent-rgb) / <alpha-value>)',
          soft: 'rgb(var(--accent-rgb) / 0.12)',
        },
        surface: {
          DEFAULT: 'rgb(var(--surface-rgb) / <alpha-value>)',
          2: 'rgb(var(--surface-2-rgb) / <alpha-value>)',
          3: 'rgb(var(--surface-3-rgb) / <alpha-value>)',
          bg: 'rgb(var(--bg-rgb) / <alpha-value>)',
        },
        line: {
          DEFAULT: 'rgb(var(--border-rgb) / <alpha-value>)',
          strong: 'rgb(var(--border-strong-rgb) / <alpha-value>)',
        },
        ink: {
          DEFAULT: 'rgb(var(--text-rgb) / <alpha-value>)',
          muted: 'rgb(var(--text-muted-rgb) / <alpha-value>)',
          subtle: 'rgb(var(--text-subtle-rgb) / <alpha-value>)',
        },
      },
      boxShadow: {
        xs: '0 1px 1px rgb(24 24 28 / 0.03)',
        soft: '0 1px 2px rgb(24 24 28 / 0.04), 0 1px 1px rgb(24 24 28 / 0.02)',
        card: '0 4px 8px -3px rgb(24 24 28 / 0.05), 0 1px 3px rgb(24 24 28 / 0.04)',
        lift: '0 8px 16px -6px rgb(24 24 28 / 0.08), 0 2px 4px rgb(24 24 28 / 0.04)',
        popover: '0 18px 40px -14px rgb(24 24 28 / 0.16), 0 6px 12px -6px rgb(24 24 28 / 0.08)',
        brand: '0 8px 20px -6px rgb(var(--brand-rgb) / 0.35), 0 2px 4px rgb(var(--brand-rgb) / 0.15)',
      },
      borderRadius: {
        xl: '0.875rem',
        '2xl': '1.125rem',
        '3xl': '1.375rem',
      },
      keyframes: {
        'fade-in': {
          '0%': { opacity: 0, transform: 'translateY(4px)' },
          '100%': { opacity: 1, transform: 'translateY(0)' },
        },
      },
      animation: {
        'fade-in': 'fade-in 200ms ease-out',
      },
    },
  },
  plugins: [
    require('@tailwindcss/forms'),
    require('@tailwindcss/typography'),
  ],
};
