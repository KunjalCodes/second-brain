import nextCoreWebVitals from "eslint-config-next/core-web-vitals";
import nextTypescript from "eslint-config-next/typescript";

const config = [
  ...nextCoreWebVitals,
  ...nextTypescript,
  {
    settings: { react: { version: "19.3" } },
    rules: {
      // Memory and document content is untrusted data: never render it as HTML.
      "react/no-danger": "error",
    },
  },
  { ignores: [".next/**", "node_modules/**", "lib/api/schema.d.ts", "playwright-report/**", "test-results/**", "next-env.d.ts"] },
];

export default config;
