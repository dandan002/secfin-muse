/** @type {import('tailwindcss').Config} */
const token = (name) => `rgb(var(--${name}) / <alpha-value>)`;

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: token("bg"),
        panel: token("panel"),
        raised: token("raised"),
        line: token("line"),
        ink: token("ink"),
        dim: token("dim"),
        faint: token("faint"),
        accent: token("accent"),
        up: token("up"),
        down: token("down"),
        gc: token("gc"),
        warm: token("warm"),
        hot: token("hot"),
        info: token("info"),
      },
      fontFamily: {
        mono: ["'JetBrains Mono'", "ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
      fontSize: {
        "2xs": ["10.5px", "14px"],
        xs: ["11.5px", "16px"],
        base: ["13px", "20px"],
      },
      borderRadius: { soft: "10px" },
    },
  },
  plugins: [],
};
