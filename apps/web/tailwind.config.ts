import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        background: "rgb(10 10 12)",
        foreground: "rgb(240 240 245)",
        panel: "rgb(20 20 24)",
        border: "rgb(40 40 48)",
        accent: "rgb(99 102 241)",
      },
    },
  },
  plugins: [],
};

export default config;
