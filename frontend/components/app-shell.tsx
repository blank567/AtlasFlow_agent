"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { ReactNode, useEffect, useState } from "react";
import { ChartLineUp, ClockCounterClockwise, Database, Desktop, GearSix, MoonStars, SquaresFour, Sun } from "@phosphor-icons/react";

const NAV_ITEMS = [
  { href: "/", label: "工作台", icon: SquaresFour },
  { href: "/runs", label: "运行记录", icon: ClockCounterClockwise },
  { href: "/analytics", label: "统计分析", icon: ChartLineUp },
  { href: "/knowledge", label: "知识库", icon: Database },
  { href: "/settings", label: "系统设置", icon: GearSix },
];

export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const [theme, setTheme] = useState<"light" | "dark">("light");
  useEffect(() => {
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const sync = () => {
      let saved: string | null = null;
      try { saved = localStorage.getItem("atlasflow.theme"); } catch { /* Storage may be unavailable. */ }
      const next = saved === "light" || saved === "dark" ? saved : media.matches ? "dark" : "light";
      document.documentElement.dataset.theme = next;
      document.documentElement.style.colorScheme = next;
      setTheme(next);
    };
    sync(); media.addEventListener("change", sync);
    return () => media.removeEventListener("change", sync);
  }, []);
  function toggleTheme() {
    const next = theme === "light" ? "dark" : "light";
    document.documentElement.dataset.theme = next;
    document.documentElement.style.colorScheme = next;
    setTheme(next);
    try { localStorage.setItem("atlasflow.theme", next); } catch { /* Theme remains usable in memory. */ }
  }
  const themeControl = <button type="button" className="themeToggle" data-theme-toggle aria-label={`切换为${theme === "light" ? "深色" : "浅色"}主题`} onClick={toggleTheme}>
    {theme === "light" ? <MoonStars size={18} aria-hidden="true" /> : <Sun size={18} aria-hidden="true" />}<span>切换外观</span>
  </button>;
  return (
    <div className="appShell">
      <aside className="sidebar">
        <Link href="/" className="brand" aria-label="AtlasFlow工作台">
          <span className="brandMark">A</span>
          <span><strong>AtlasFlow</strong><small>研究运行台</small></span>
        </Link>
        <nav className="navList" aria-label="主导航">
          {NAV_ITEMS.map((item) => {
            const active = item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
            const Icon = item.icon;
            return (
              <Link key={item.href} href={item.href} aria-current={active ? "page" : undefined} className={active ? "navItem active" : "navItem"}>
                <Icon size={20} weight={active ? "duotone" : "regular"} aria-hidden="true" /><span>{item.label}</span>
              </Link>
            );
          })}
        </nav>
        <div className="sidebarUtilities">{themeControl}<div className="sidebarFoot"><Desktop size={16} aria-hidden="true" /><span>本地工作区</span><small>v0.6.0</small></div></div>
      </aside>
      <div className="pageFrame">
        <header className="mobileHeader">
          <Link href="/" className="brand"><span className="brandMark">A</span><strong>AtlasFlow</strong></Link>
          {themeControl}
        </header>
        {children}
      </div>
      <nav className="mobileNav" aria-label="移动端导航">
        {NAV_ITEMS.map((item) => {
          const active = item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
          const Icon = item.icon;
          return <Link key={item.href} href={item.href} aria-current={active ? "page" : undefined} className={active ? "active" : ""}><Icon size={20} weight={active ? "duotone" : "regular"} aria-hidden="true" /><span>{item.label}</span></Link>;
        })}
      </nav>
    </div>
  );
}
