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

  if (!report.trim()) return <EmptyState title="报告尚未生成" description="成稿后支持阅读、导出与打印。" />;

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
      setNotice(`${format === "md" ? "Markdown" : "HTML"}下载已发起，请选择E盘保存。`);
    } catch (error) {
      setExportError(error instanceof Error ? error.message : "下载失败，请重试。");
    }
  }

  return <section className="reportWorkspace" aria-label="研究报告阅读器">
    <style>{REPORT_DOCUMENT_STYLES}</style>
    <div className="reportToolbar">
      <div><h2>研究报告</h2><p>预计阅读 {readingMinutes} 分钟</p></div>
      <div className="reportActions">
        <button type="button" className="secondaryButton" onClick={() => exportReport("md")}>下载Markdown</button>
        <button type="button" className="primaryButton" onClick={() => exportReport("html")}>下载HTML</button>
        <button type="button" className="secondaryButton" onClick={() => { setNotice("在打印窗口选择“另存为PDF”，保存至E盘。"); window.print(); }}>打印/另存PDF</button>
      </div>
    </div>
    <p className="reportDownloadHint">HTML保留排版并支持离线阅读，Markdown可编辑。请选择E盘保存。</p>
    <div className="reportActionStatus" role="status">{notice}</div>
    {exportError && <p className="reportExportError" role="alert">{exportError}</p>}
    <div className={`reportReader ${sections.length ? "" : "reportReaderNoOutline"}`}>
      {sections.length > 0 && <aside className="reportOutline">
        <details ref={outlineDetails} open><summary>报告目录 <span>{sections.length}</span></summary>
          <nav aria-label="报告目录">{sections.map((section) => <a key={section.id} href={`#${section.id}`} className={section.id === activeSection ? "active" : undefined} aria-current={section.id === activeSection ? "location" : undefined} style={{ paddingLeft: `${12 + (section.depth - baseDepth) * 12}px` }} onClick={() => setActiveSection(section.id)}>{section.title}</a>)}</nav>
        </details>
      </aside>}
      <article className="reportPaper" ref={paper}>
        <header className="reportPaperHeader">
          <div className="reportPaperIdentity"><strong>AtlasFlow</strong><span>研究报告</span></div>
          <h1>{run.query}</h1>
          <div className="reportPaperMeta"><span>{stateLabel}</span>{date && <time dateTime={date}>{run.completed_at ? "完成于" : "记录更新于"} {formatDate(date)}</time>}</div>
          {!completed && <p className="reportPaperNotice">当前版本尚未完成最终验收。HTML与打印保留此状态，Markdown导出报告原文。</p>}
          {run.status === "completed_with_warnings" && <p className="reportPaperNotice">本次研究未通过最终验收；以下是保留的草稿，请结合局限与参考文献阅读。{run.warnings.length > 0 && run.warnings.join("；")}</p>}
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
        <footer className="reportPaperFooter"><span>AtlasFlow多Agent研究</span><span>运行编号 {run.id}</span><span>信息时效与适用范围请以报告参考文献为准。</span></footer>
      </article>
    </div>
  </section>;
}
