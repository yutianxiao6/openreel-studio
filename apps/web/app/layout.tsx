import type { Metadata, Viewport } from "next";
import "./globals.css";
import { DownloadFeedback } from "@/components/common/DownloadFeedback";

const basePath = process.env.NEXT_PUBLIC_BASE_PATH ?? "";

export const metadata: Metadata = {
  title: "OpenReel Studio",
  description: "聊天式视频智能创作工作台",
  icons: {
    icon: [
      { url: `${basePath}/favicon.ico`, sizes: "any" },
      { url: `${basePath}/icon.png`, type: "image/png", sizes: "512x512" },
    ],
    apple: [{ url: `${basePath}/apple-icon.png`, sizes: "180x180" }],
  },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  maximumScale: 1,
  viewportFit: "cover",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="zh-CN">
      <body className="min-h-screen overflow-hidden antialiased">
        {children}
        <DownloadFeedback />
      </body>
    </html>
  );
}
