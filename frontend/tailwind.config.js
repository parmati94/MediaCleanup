import colors from 'tailwindcss/colors';

export default {
  darkMode: 'media',
  content: [
    "./index.html",
    "./partials/**/*.html",
    "./js/**/*.js",
  ],
  theme: {
    extend: {
      colors: {
        // Cooler, considered neutrals everywhere gray-* is used.
        gray: colors.slate,
        // "Reclaim" accent — teal. Highlights the space you can get back.
        primary: {
          50: '#f0fdfa',
          100: '#ccfbf1',
          200: '#99f6e4',
          300: '#5eead4',
          400: '#2dd4bf',
          500: '#14b8a6',
          600: '#0d9488',
          700: '#0f766e',
          800: '#115e59',
          900: '#134e4a',
          950: '#042f2e',
        },
      },
      fontFamily: {
        sans: ['"IBM Plex Sans"', 'ui-sans-serif', 'system-ui', '-apple-system', 'Segoe UI', 'Roboto', 'sans-serif'],
        mono: ['"IBM Plex Mono"', 'ui-monospace', 'SFMono-Regular', 'monospace'],
      },
      boxShadow: {
        soft: '0 1px 2px 0 rgba(2, 6, 23, 0.04), 0 6px 20px -6px rgba(2, 6, 23, 0.10)',
      },
    },
  },
  plugins: [],
}
