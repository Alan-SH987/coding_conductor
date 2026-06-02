import type { Config } from "tailwindcss";

const token = (name: string) => `rgb(var(${name}) / <alpha-value>)`;

const config: Config = {
  darkMode: "class",
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./lib/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        background: token("--background"),
        foreground: token("--foreground"),
        card: token("--card"),
        "card-foreground": token("--card-foreground"),
        muted: token("--muted"),
        "muted-foreground": token("--muted-foreground"),
        secondary: token("--secondary"),
        "secondary-foreground": token("--secondary-foreground"),
        border: token("--border"),
        input: token("--input"),
        ring: token("--ring"),
        primary: token("--primary"),
        "primary-foreground": token("--primary-foreground"),
      },
    },
  },
  plugins: [],
};

export default config;
