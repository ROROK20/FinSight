import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./app/**/*.{js,ts,jsx,tsx,mdx}",
    "./components/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        paper: {
          DEFAULT: "var(--paper)",
          deep: "var(--paper-deep)",
          lift: "var(--paper-lift)",
        },
        ink: {
          DEFAULT: "var(--ink)",
          soft: "var(--ink-soft)",
        },
        hair: "var(--hair)",
        oxblood: {
          DEFAULT: "var(--oxblood)",
          deep: "var(--oxblood-deep)",
        },
      },
      fontFamily: {
        serif: ["var(--font-serif)", "Georgia", "Times New Roman", "serif"],
        stat: ["var(--font-stat)", "Arial Narrow", "Helvetica Neue", "sans-serif"],
      },
    },
  },
  plugins: [],
};
export default config;
