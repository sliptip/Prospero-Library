import tailwindcss from '@tailwindcss/vite'
import { defineConfig } from 'vite'

const proxy = Object.fromEntries(['/api', '/media'].map(path => [
  path, { target: 'http://127.0.0.1:8000', changeOrigin: true },
]))

export default defineConfig({ plugins: [tailwindcss()], server: { proxy } })
