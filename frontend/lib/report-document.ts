// Shared by the live reader and self-contained HTML export. No remote fonts/assets.
export const REPORT_DOCUMENT_STYLES = `
.reportPaper { box-sizing: border-box; width: 100%; min-width: 0; padding: 46px clamp(24px, 5vw, 64px); background: #fff; color: #27384a; font-family: "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; overflow-wrap: anywhere; }
.reportPaper * { box-sizing: border-box; }
.reportPaperHeader { padding-bottom: 28px; margin-bottom: 32px; border-bottom: 2px solid #244960; }
.reportPaperIdentity { display: flex; justify-content: space-between; gap: 18px; color: #526879; font-size: 12px; }
.reportPaperIdentity strong { color: #244960; font-size: 14px; }
.reportPaperHeader h1 { margin: 24px 0 20px; color: #18354b; font-family: "Source Han Serif SC", "Noto Serif CJK SC", "Songti SC", SimSun, serif; font-size: clamp(25px, 2.5vw, 34px); font-weight: 700; line-height: 1.55; letter-spacing: -.025em; text-wrap: balance; }
.reportPaperMeta { display: flex; flex-wrap: wrap; gap: 8px 24px; color: #627284; font-size: 12px; line-height: 1.7; }
.reportPaperNotice { margin: 18px 0 0; padding: 10px 14px; background: #fff7e8; border-left: 3px solid #b57c24; color: #79521b; font-size: 13px; line-height: 1.8; }
.reportProse { font-size: 15px; line-height: 1.95; }
.reportProse > :first-child { margin-top: 0; }
.reportProse :is(h1,h2,h3,h4,h5,h6) { color: #18354b; line-height: 1.5; scroll-margin-top: 28px; break-after: avoid; }
.reportProse h1 { margin: 38px 0 16px; font-size: 25px; }
.reportProse h2 { margin: 36px 0 14px; padding-bottom: 10px; border-bottom: 1px solid #dce4e9; font-size: 21px; }
.reportProse h3 { margin: 26px 0 10px; font-size: 17px; }
.reportProse :is(h4,h5,h6) { margin: 22px 0 8px; font-size: 15px; }
.reportProse p { margin: 12px 0; }
.reportProse :is(ul,ol) { margin: 14px 0; padding-left: 1.7em; }
.reportProse li { margin: 7px 0; padding-left: .25em; }
.reportProse li::marker { color: #58758a; }
.reportProse li > p { margin: 6px 0; }
.reportProse strong { color: #18354b; font-weight: 650; }
.reportProse a { color: #235d8c; text-decoration: underline; text-underline-offset: 3px; }
.reportPaper a:focus-visible { outline: 2px solid #235d8c; outline-offset: 4px; }
.reportProse blockquote { margin: 24px 0; padding: 12px 20px; border-left: 3px solid #63859b; background: #f3f7fa; color: #486073; }
.reportProse blockquote > :first-child { margin-top: 0; }
.reportProse blockquote > :last-child { margin-bottom: 0; }
.reportProse hr { margin: 32px 0; border: 0; border-top: 1px solid #dce4e9; }
.reportTableScroll { overflow-x: auto; max-width: 100%; margin: 24px 0; border: 1px solid #dce4e9; border-radius: 6px; }
.reportTableScroll:focus-visible { outline: 2px solid #235d8c; outline-offset: 3px; }
.reportProse table { width: 100%; border-collapse: collapse; font-size: 13px; line-height: 1.75; }
.reportProse :is(th,td) { min-width: 100px; padding: 12px 15px; text-align: left; vertical-align: top; border-bottom: 1px solid #e4ebf0; }
.reportProse th { background: #edf3f7; color: #244960; font-weight: 650; }
.reportProse tbody tr:nth-child(even) { background: #f8fafc; }
.reportProse tbody tr:last-child td { border-bottom: 0; }
.reportProse code { padding: 2px 5px; background: #eff3f6; border-radius: 3px; font-family: Consolas, monospace; font-size: .87em; }
.reportProse pre { overflow-x: auto; padding: 18px 20px; background: #f2f5f8; border: 1px solid #dce4e9; border-radius: 6px; line-height: 1.7; }
.reportProse pre code { padding: 0; background: none; }
.reportProse .task-list-item { list-style: none; }
.reportPaperFooter { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 8px 20px; margin-top: 44px; padding-top: 18px; border-top: 1px solid #dce4e9; color: #627284; font-size: 11px; line-height: 1.8; }
@media (max-width: 640px) {
  .reportPaper { padding: 28px 20px; }
  .reportPaperHeader h1 { font-size: 25px; }
  .reportProse { font-size: 14px; }
  .reportProse h1 { font-size: 22px; }
  .reportProse h2 { font-size: 19px; }
}
@media (max-width: 860px) {
  .reportProse :is(h1,h2,h3,h4,h5,h6) { scroll-margin-top: 80px; }
}
@media print {
  @page { size: A4; margin: 17mm 15mm; }
  .reportPaper { max-width: none !important; padding: 0 !important; border: 0 !important; box-shadow: none !important; }
  .reportPaperHeader h1 { font-size: 23pt; }
  .reportProse { font-size: 10.5pt; line-height: 1.8; }
  .reportProse :is(p,li) { orphans: 3; widows: 3; }
  .reportProse :is(pre,blockquote,tr) { break-inside: avoid; }
  .reportProse thead { display: table-header-group; }
  .reportTableScroll { overflow: visible; }
  .reportProse table { table-layout: fixed; font-size: 9pt; }
  .reportProse :is(th,td) { min-width: 0; padding: 7px; }
  .reportProse pre { white-space: pre-wrap; overflow-wrap: anywhere; }
}
`;

type HeadingNode = { type: string; depth?: number; children?: HeadingNode[]; data?: { hProperties?: Record<string, unknown> } };

// Work on parsed Markdown, not regex: code fences never become phantom TOC entries.
export function reportHeadingIds() {
  return (tree: HeadingNode) => {
    let index = 0;
    const visit = (node: HeadingNode) => {
      if (node.type === "heading") {
        node.data = { ...node.data, hProperties: { ...node.data?.hProperties, id: `report-section-${++index}` } };
      }
      node.children?.forEach(visit);
    };
    visit(tree);
  };
}

export function safeReportUrl(url?: string): string | undefined {
  if (!url) return undefined;
  const value = url.trim();
  if (value.startsWith("#")) return value;
  if (/^(https?:\/\/|mailto:)/i.test(value)) return value;
  return undefined;
}

export function reportFilename(query: string, runId: string): string {
  const title = query.replace(/[\u0000-\u001f\u007f<>:"/\\|?*]/g, " ").replace(/\s+/g, " ").trim().slice(0, 60).replace(/[. ]+$/g, "");
  const id = runId.replace(/[^a-zA-Z0-9-]/g, "").slice(0, 8) || "export";
  return `AtlasFlow-${title || "研究报告"}-${id}`;
}

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]!);
}

// renderedArticle must come from ReportView's safe ReactMarkdown DOM, never raw model HTML.
export function buildReportHtml(title: string, renderedArticle: string): string {
  return `<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>${escapeHtml(title)} — AtlasFlow</title>
<style>body{margin:0;padding:40px 16px;background:#edf2f6}.reportPaper{max-width:900px;margin:auto}a{color:inherit}@media print{body{padding:0;background:white}}${REPORT_DOCUMENT_STYLES}</style>
</head><body>${renderedArticle}</body></html>`;
}

export function downloadReport(content: string, filename: string, mime: string): void {
  const blob = new Blob(["\uFEFF", content], { type: mime });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  try { link.click(); } finally {
    link.remove();
    // Allow the browser to begin the download before releasing the URL.
    setTimeout(() => URL.revokeObjectURL(url), 10_000);
  }
}
