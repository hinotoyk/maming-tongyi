import { defineConfig } from 'astro/config';
import tailwindcss from '@tailwindcss/vite';

// 项目页部署在 https://hinotoyk.github.io/maming-tongyi/ 的子路径下。
// 绑自定义域名后：把 SITE_BASE 设为 '/'，并在 public/CNAME 写入域名。
const base = process.env.SITE_BASE || '/maming-tongyi/';

export default defineConfig({
  site: 'https://hinotoyk.github.io',
  base,
  trailingSlash: 'ignore',
  vite: {
    plugins: [tailwindcss()],
  },
});
