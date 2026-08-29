/** @type {import('next').NextConfig} */
const nextConfig = {
  output: 'export',
  images: { unoptimized: true },
  // 关闭 trailingSlash：避免浏览器 fetch 把 /api/projects 自动变 /api/projects/
  // 导致 FastAPI 精确匹配失败、StaticFiles fallback 到 404 index.html
  trailingSlash: false,
  reactStrictMode: true,
  // export 模式下开发服务器（next dev 3001）仍需转发 API 到后端 8000
  // 让同域部署 + next dev 都能正确工作
  async rewrites() {
    return [
      {
        source: '/api/:path*',
        destination: 'http://127.0.0.1:8000/api/:path*',
      },
    ];
  },
};

module.exports = nextConfig;
