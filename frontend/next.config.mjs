/** @type {import('next').NextConfig} */
// Atlas-Lite 後端預設 8020（與 Atlas 的 8014、V5 的 8004 並存）。
// BACKEND_PORT 環境變數可覆寫。
const BACKEND_PORT = process.env.BACKEND_PORT || '8020'
const nextConfig = {
  reactStrictMode: false,
  async rewrites() {
    return [
      {
        source: '/api/backend/:path*',
        destination: `http://localhost:${BACKEND_PORT}/:path*`,
      },
    ]
  },
}

export default nextConfig
