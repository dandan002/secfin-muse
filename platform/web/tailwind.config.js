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
        rule: token("rule"),
        strong: token("strong"),
        soft: token("soft"),
        "on-accent": token("on-accent"),
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
        sans: ["'Instrument Sans'", "'Helvetica Neue'", "Arial", "sans-serif"],
      },
      fontSize: {
        "2xs": ["11px", "14px"],  // caption
        xs: ["12px", "16px"],     // label
        sm: ["13px", "18px"],     // body
        base: ["13px", "18px"],
        md: ["14px", "20px"],     // heading
      },
      borderRadius: { soft: "6px" },
    },
  },
  plugins: [],
};
