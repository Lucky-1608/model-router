import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./src/pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/components/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/app/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        gateway: {
          bg: "#090D16",
          panel: "#111827",
          border: "#1F2937",
          accent: "#3B82F6",
        },
      },
    },
  },
  plugins: [],
};

export default config;

