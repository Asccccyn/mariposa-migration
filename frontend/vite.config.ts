// 工程配置（程知行 2026-10-13 接入 mariposa）：
// 原稿 vite.config 的 Figma Make 专属插件（site.json 注入/错误重放/
// stories kit）是 Figma 预览环境的开发辅助，接入仓库后不再需要；
// 新增 /api 与 /oauth 开发代理指向本地 mariposa 实例。
// 设计内容（src/App.tsx 画风/布局）未动。
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'node:path'

const BACKEND = process.env.MARIPOSA_DEV_BACKEND || 'http://127.0.0.1:18780'

export default defineConfig(({ mode }) => {
  const emitSourcemaps = mode === 'development'
  return {
    build: {
      sourcemap: emitSourcemaps ? 'inline' : false,
      minify: !emitSourcemaps,
    },
    plugins: [react(), tailwindcss()],
    resolve: {
      alias: { '@': path.resolve(__dirname, './src') },
    },
    server: {
      host: '127.0.0.1',
      port: parseInt(process.env.PORT || '5180'),
      strictPort: true,
      proxy: {
        '/api': { target: BACKEND, changeOrigin: false },
        '/oauth': { target: BACKEND, changeOrigin: false },
      },
    },
    preview: {
      host: '127.0.0.1',
      port: parseInt(process.env.PORT || '5180'),
    },
  }
})
