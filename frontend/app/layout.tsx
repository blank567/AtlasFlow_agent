import type { Metadata } from "next";
import { AppShell } from "../components/app-shell";
import "@xyflow/react/dist/style.css";
import "./globals.css";

export const metadata: Metadata = {
  title: { default: "AtlasFlow · Agent Observatory", template: "%s · AtlasFlow" },
  description: "可观测、可审计、可追溯的多 Agent 研究平台",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body><AppShell>{children}</AppShell></body>
    </html>
  );
}
