"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { ReactNode } from "react";

const NAV_ITEMS = [
  { href: "/", label: "工作台" },
  { href: "/runs", label: "运行记录" },
  { href: "/analytics", label: "统计分析" },
  { href: "/settings", label: "系统设置" },
];

export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  return (
    <div className="appShell">
      <aside className="sidebar">
        <Link href="/" className="brand" aria-label="AtlasFlow 工作台">
          <span className="brandMark">A</span>
          <span><strong>AtlasFlow</strong><small>研究运行台</small></span>
        </Link>
        <nav className="navList" aria-label="主导航">
          {NAV_ITEMS.map((item) => {
            const active = item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
            return (
              <Link key={item.href} href={item.href} className={active ? "navItem active" : "navItem"}>
                {item.label}
              </Link>
            );
          })}
        </nav>
        <div className="sidebarFoot">
          <span className="onlineDot" />
          <span>本地工作区</span>
          <small>v0.4.5</small>
        </div>
      </aside>
      <div className="pageFrame">
        <header className="mobileHeader">
          <Link href="/" className="brand"><span className="brandMark">A</span><strong>AtlasFlow</strong></Link>
          <span className="localBadge"><span className="onlineDot" /> 本地</span>
        </header>
        {children}
      </div>
      <nav className="mobileNav" aria-label="移动端导航">
        {NAV_ITEMS.map((item) => {
          const active = item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
          return <Link key={item.href} href={item.href} className={active ? "active" : ""}>{item.label}</Link>;
        })}
      </nav>
    </div>
  );
}
