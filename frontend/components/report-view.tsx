"use client";

import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { formatDate } from "../lib/format";
import { buildReportHtml, downloadReport, reportFilename, reportHeadingIds, REPORT_DOCUMENT_STYLES, safeReportUrl } from "../lib/report-document";
import { RunRecord } from "../lib/types";
import { EmptyState } from "./ui";

type Section = { id: string; title: string; depth: number };

export function ReportView({ run }: { run: RunRecord }) {
  const paper = useRef<HTMLElement>(null);
  const outlineDetails = useRef<HTMLDetailsElement>(null);
  const [sections, setSections] = useState<Section[]>([]);
  const [activeSection, setActiveSection] = useState("");
  const [notice, setNotice] = useState("");
  const [exportError, setExportError] = useState("");
  const report = run.report ?? "";

  useEffect(() => {
    const desktop = window.matchMedia("(min-width: 1181px)");
    const sync = () => { if (outlineDetails.current) outlineDetails.current.open = desktop.matches; };
    sync();
    desktop.addEventListener("change", sync);
    return () => desktop.removeEventListener("change", sync);
  }, [report, sections.length]);

  useEffect(() => {
    const headings = [...(paper.current?.querySelectorAll<HTMLElement>(".reportProse h1, .reportProse h2, .reportProse h3") ?? [])];
    setSections(headings.map((heading) => ({ id: heading.id, title: heading.textContent ?? "", depth: Number(heading.tagName[1]) })));
    setActiveSection(headings[0]?.id ?? "");
    if (!headings.length || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver((entries) => {
      const visible = entries.filter((entry) => entry.isIntersecting).sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
      if (visible[0]) setActiveSection(visible[0].target.id);
    }, { rootMargin: "0px 0px -60% 0px" });
    headings.forEach((heading) => observer.observe(heading));
    return () => observer.disconnect();
  }, [report]);

  if (!report.trim()) return <EmptyState title="报告尚未生成" description="报告生成后，可以在这里阅读、下载或打印。" />;

  const completed = run.status === "completed" || run.status === "completed_with_warnings";
  const stateLabel = run.status === "completed" ? "已通过验收" : "未通过最终验收";
  const filename = reportFilename(run.query, run.id);
  const date = run.completed_at ?? run.updated_at ?? run.created_at;
  const readingMinutes = Math.max(1, Math.ceil(report.length / 500));
  const baseDepth = Math.min(...sections.map((section) => section.depth));

  function exportReport(format: "md" | "html") {
    setExportError("");
    try {
      if (format === "html" && !paper.current) throw new Error("报告尚未就绪，请稍后重试。");
      const content = format === "md" ? report : buildReportHtml(run.query, paper.current!.outerHTML);
      downloadReport(content, `${filename}.${format}`, format === "md" ? "text/markdown;charset=utf-8" : "text/html;charset=utf-8");
      setNotice(`已发起 ${format === "md" ? "Markdown" : "HTML"} 下载；请在浏览器中选择 E 盘保存位置。`);
    } catch (error) {
      setExportError(error instanceof Error ? error.message : "下载失败，请重试。");
    }
  }

  return <section className="reportWorkspace" aria-label="研究报告阅读器">
    <style>{REPORT_DOCUMENT_STYLES}</style>
    <div className="reportToolbar">
      <div><h2>研究报告</h2><p>约 {readingMinutes} 分钟阅读 · 下载不产生模型调用</p></div>
      <div className="reportActions">
        <button type="button" className="secondaryButton" onClick={() => exportReport("md")}>下载 Markdown</button>
        <button type="button" className="primaryButton" onClick={() => exportReport("html")}>下载 HTML</button>
        <button type="button" className="secondaryButton" onClick={() => { setNotice("在打印窗口中选择“另存为 PDF”，并保存到 E 盘。"); window.print(); }}>打印 / 存为 PDF</button>
      </div>
    </div>
    <p className="reportDownloadHint">HTML 保留排版，可离线打开；Markdown 便于编辑。请将浏览器下载位置设为 E 盘，或开启“下载前询问保存位置”。</p>
    <div className="reportActionStatus" role="status">{notice}</div>
    {exportError && <p className="reportExportError" role="alert">{exportError}</p>}
    <div className={`reportReader ${sections.length ? "" : "reportReaderNoOutline"}`}>
      {sections.length > 0 && <aside className="reportOutline">
        <details ref={outlineDetails} open><summary>本篇目录 <span>{sections.length}</span></summary>
          <nav aria-label="报告目录">{sections.map((section) => <a key={section.id} href={`#${section.id}`} className={section.id === activeSection ? "active" : undefined} aria-current={section.id === activeSection ? "location" : undefined} style={{ paddingLeft: `${12 + (section.depth - baseDepth) * 12}px` }} onClick={() => setActiveSection(section.id)}>{section.title}</a>)}</nav>
        </details>
      </aside>}
      <article className="reportPaper" ref={paper}>
        <header className="reportPaperHeader">
          <div className="reportPaperIdentity"><strong>AtlasFlow</strong><span>研究报告</span></div>
          <h1>{run.query}</h1>
          <div className="reportPaperMeta"><span>{stateLabel}</span>{date && <time dateTime={date}>{run.completed_at ? "完成于" : "记录更新于"} {formatDate(date)}</time>}</div>
          {!completed && <p className="reportPaperNotice">当前内容尚未完成最终验收，仅供参考；HTML 与打印版本会保留此提示，Markdown 仅包含报告原文。</p>}
          {run.status === "completed_with_warnings" && <p className="reportPaperNotice">本次研究未通过最终验收；以下是保留的草稿，请结合局限与来源阅读。{run.warnings.length > 0 && run.warnings.join("；")}</p>}
        </header>
        <div className="reportProse"><ReactMarkdown remarkPlugins={[remarkGfm, reportHeadingIds]} skipHtml urlTransform={(url) => safeReportUrl(url) ?? ""} components={{
          a: ({ href, children }) => href ? <a href={href} target={href.startsWith("#") ? undefined : "_blank"} rel={href.startsWith("#") ? undefined : "noopener noreferrer"}>{children}</a> : <span>{children}</span>,
          p: ({ children }) => {
            const first = Array.isArray(children) ? children[0] : children;
            const reference = typeof first === "string" && /^\[\d+\]\s/.test(first);
            return <p className={reference ? "reportReferenceEntry" : undefined}>{children}</p>;
          },
          table: ({ children }) => <div className="reportTableScroll" role="region" aria-label="报告数据表格" tabIndex={0}><table>{children}</table></div>,
          img: ({ src, alt }) => typeof src === "string" && safeReportUrl(src) ? <a href={src} target="_blank" rel="noopener noreferrer">图片：{alt || "查看原图"}</a> : <span>{alt}</span>,
        }}>{report}</ReactMarkdown></div>
        <footer className="reportPaperFooter"><span>AtlasFlow 多 Agent 研究</span><span>运行编号 {run.id}</span><span>信息时效与适用范围请以报告来源为准。</span></footer>
      </article>
    </div>
  </section>;
}
