"use client";

import { ReactNode, useEffect } from "react";

export function DetailDrawer({ title, subtitle, onClose, children }: { title: string; subtitle?: string; onClose: () => void; children: ReactNode }) {
  useEffect(() => {
    const close = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, [onClose]);
  return <div className="drawerBackdrop" role="presentation" onMouseDown={(event) => { if (event.currentTarget === event.target) onClose(); }}><aside className="detailDrawer" role="dialog" aria-modal="true" aria-label={title}><header><div><span className="eyebrow">执行详情</span><h2>{title}</h2>{subtitle && <p>{subtitle}</p>}</div><button onClick={onClose} aria-label="关闭详情">×</button></header><div className="drawerContent">{children}</div></aside></div>;
}

export function DrawerSection({ title, children }: { title: string; children: ReactNode }) {
  return <section className="drawerSection"><h3>{title}</h3>{children}</section>;
}
