import type { Config } from 'tailwindcss';

const config: Config = {
  content: ['./app/**/*.{ts,tsx}', './components/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        brand: '#003E60',
        danger: '#DF0000',
      },
    },
  },
  plugins: [],
};

export default config;
