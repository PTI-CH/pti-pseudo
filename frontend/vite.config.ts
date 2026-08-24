/// <reference types="vitest" />
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Das Backend ist FastAPI/uvicorn auf Port 8000 (nicht 3000 wie beim
// Uebersetzer, der ein Node-Backend hat).
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
  test: {
    // jsdom statt einer echten Browser-Umgebung: die Panels werden auf ihre
    // Logik geprueft (welcher Wert geht an die API), nicht auf Darstellung.
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
  },
});
