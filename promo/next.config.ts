import type {NextConfig} from "next"
import path from "node:path"

const basePath = process.env.NEXT_PUBLIC_BASE_PATH ?? "/promo"

const nextConfig: NextConfig = {
  basePath,
  output: "standalone",
  outputFileTracingRoot: path.join(__dirname, ".."),
  reactStrictMode: true,
}

export default nextConfig
