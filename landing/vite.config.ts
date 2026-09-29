import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

export default defineConfig({
  plugins: [react()],
  server: {
    // `wrangler pages dev` serves the Functions on 8788; run it beside `vite`.
    proxy: { "/api": "http://127.0.0.1:8788" },
  },
})
