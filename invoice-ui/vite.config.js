import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The app calls the backend using relative paths like "/api/invoices" -
// never a hardcoded http://localhost:8000. That means the exact same code
// works in two different setups without any changes:
//
//   1. Development: this dev server runs on its own port (5173) and the
//      proxy below forwards any /api/* request to FastAPI on port 8000.
//   2. Production: FastAPI serves this app's built files directly (see
//      the backend's static file mount), so the frontend and the API are
//      on the same origin and "/api/..." just works with no proxy needed.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
  },
})
