import type { Metadata } from "next";
import localFont from "next/font/local";
import { AppShell } from "../components/app-shell";
import "@xyflow/react/dist/style.css";
import "./globals.css";
import "./report.css";
import "./visual-system.css";
import "./evaluations.css";
import "./evaluation-detail-dialog.css";

const geist = localFont({
  src: "../node_modules/@fontsource-variable/geist/files/geist-latin-wght-normal.woff2",
  variable: "--font-geist",
  weight: "100 900",
  display: "swap",
  adjustFontFallback: false,
  fallback: ["Microsoft YaHei", "PingFang SC", "Segoe UI", "sans-serif"],
});

const themeScript = `try{const saved=localStorage.getItem('atlasflow.theme');const theme=saved==='light'||saved==='dark'?saved:matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light';document.documentElement.dataset.theme=theme;document.documentElement.style.colorScheme=theme}catch{}`;

export const metadata: Metadata = {
  title: { default: "AtlasFlow·研究工作台", template: "%s·AtlasFlow" },
  description: "可观测、可审计、可追溯的多Agent研究平台",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN" suppressHydrationWarning>
      <head><script dangerouslySetInnerHTML={{ __html: themeScript }} /></head>
      <body className={geist.variable}><AppShell>{children}</AppShell></body>
    </html>
  );
}
