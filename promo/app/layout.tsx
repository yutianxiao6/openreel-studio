import type {Metadata} from "next"
import type {ReactNode} from "react"

import "./globals.css"

export const metadata: Metadata = {
  title: "OpenReel Studio — 产品介绍",
  description: "OpenReel Studio 动画产品介绍与视频宣传演示。",
}

export default function RootLayout({children}: {children: ReactNode}) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  )
}
