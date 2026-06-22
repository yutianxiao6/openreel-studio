import type { NextConfig } from "next";
import path from "node:path";

const basePath = process.env.NEXT_PUBLIC_BASE_PATH ?? "";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  output: "standalone",
  outputFileTracingRoot: path.join(__dirname, "../.."),
  compress: false,
  transpilePackages: ["@drama/shared"],
  basePath,
  async rewrites() {
    const apiBase = process.env.INTERNAL_API_BASE_URL ?? "http://localhost:8000";
    return [
      {
        source: "/api/:path*",
        destination: `${apiBase}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
