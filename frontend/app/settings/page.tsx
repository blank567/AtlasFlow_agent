"use client";

import { useEffect, useRef, useState } from "react";
import { ArrowSquareOut, Check, Copy, PlugsConnected } from "@phosphor-icons/react";
import { checkLangSmith, getSettingsStatus } from "../../lib/api";
import { formatBytes, formatDate } from "../../lib/format";
import { SettingsStatus } from "../../lib/types";
import { EmptyState, LoadingBlock, PageHeader, Panel } from "../../components/ui";

const CONFIG_TEMPLATE = `# LangSmith配置（仅后端读取）
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=lsv2_...
LANGSMITH_PROJECT=atlasflow
LANGSMITH_ENDPOINT=https://api.smith.langchain.com
LANGSMITH_TRACE_CONTENT=true`;
const STATE_LABELS: Record<string,string> = { ready:"连接已验证", disabled:"追踪未启用", not_configured:"待配置", unreachable:"连接失败", error:"连接失败" };

export default function SettingsPage() {
  const [status, setStatus] = useState<SettingsStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);
  const [copied, setCopied] = useState(false);
  const requestId = useRef(0);
  const resetTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  async function load() {
    const current = ++requestId.current;
    try { const value = await getSettingsStatus(); if (current === requestId.current) { setStatus(value); setError(null); } }
    catch (reason) { if (current === requestId.current) setError(reason instanceof Error ? reason.message : "配置状态读取失败"); }
  }
  useEffect(() => {
    void load();
    return () => { requestId.current += 1; if (resetTimer.current) clearTimeout(resetTimer.current); };
  }, []);
  async function check() {
    const current = ++requestId.current;
    setChecking(true); setError(null);
    try {
      const result = await checkLangSmith();
      if (current !== requestId.current) return;
      if (result && "langsmith" in result) setStatus(result as SettingsStatus);
      else setStatus((previous) => ({ ...previous, langsmith: result as SettingsStatus["langsmith"] }));
    } catch (reason) { if (current === requestId.current) setError(reason instanceof Error ? reason.message : "追踪连接验证失败"); }
    finally { if (current === requestId.current) setChecking(false); }
  }
  async function copyTemplate() {
    try {
      await navigator.clipboard.writeText(CONFIG_TEMPLATE); setCopied(true);
      if (resetTimer.current) clearTimeout(resetTimer.current);
      resetTimer.current = setTimeout(() => setCopied(false), 1800);
    } catch { setError("配置模板复制失败，请检查浏览器剪贴板权限。"); }
  }
  const langsmith = status?.langsmith;
  const state = langsmith?.state ?? (langsmith?.configured ? langsmith.enabled ? "ready" : "disabled" : "not_configured");
  const stateLabel = state === "ready" && langsmith?.connection_status !== "reachable" ? "配置就绪，待验证" : STATE_LABELS[state] ?? state;
  return <main className="pageContent settingsPage">
    <PageHeader title="系统设置" description="模型接入、追踪服务与本地持久化状态。" actions={<span className="softBadge">v{status?.app_version ?? "0.7.0"}</span>} />
    {error && <div className="inlineAlert errorAlert" role="alert"><strong>操作未完成</strong><span>{error}</span></div>}
    {!status ? error ? <Panel><EmptyState title="配置状态读取失败" description="后端恢复连接后可重新加载配置状态。" /><button className="secondaryButton" type="button" onClick={() => void load()}>重新加载</button></Panel> : <LoadingBlock label="正在读取系统配置" /> : <div className="settingsGrid">
      <Panel title="LangSmith追踪" meta={<span className={"connectionBadge connection-" + state}><i />{stateLabel}</span>} className="settingsMain">
        <p className="settingsIntro">按运行关联追踪片段，查看Agent调用树、执行耗时与模型用量。</p>
        <dl className="settingsList">
          <div><dt>追踪状态</dt><dd>{langsmith?.enabled ? "已启用" : "未启用"}</dd></div>
          <div><dt>接入凭据</dt><dd>{langsmith?.configured ? "已配置" : "未配置"}</dd></div>
          <div><dt>项目</dt><dd>{langsmith?.project ?? "未记录"}</dd></div>
          <div><dt>服务端点</dt><dd>{langsmith?.endpoint ?? "未记录"}</dd></div>
          <div><dt>内容策略</dt><dd>{langsmith?.trace_content === true || langsmith?.trace_content === "full" ? "完整内容" : "脱敏元数据"}</dd></div>
          <div><dt>最近验证</dt><dd>{formatDate(langsmith?.last_checked_at)}</dd></div>
        </dl>
        {langsmith?.message && <div className="inlineAlert neutralAlert"><span>{langsmith.message}</span></div>}
        <div className="settingsActions"><button className="primaryButton" type="button" disabled={checking} onClick={() => void check()}><PlugsConnected size={17} aria-hidden="true" />{checking ? "正在验证" : "验证连接"}</button><button className="secondaryButton" type="button" onClick={() => void copyTemplate()}>{copied ? <Check size={17} aria-hidden="true" /> : <Copy size={17} aria-hidden="true" />}{copied ? "已复制" : "复制配置模板"}</button></div>
        <p className="privacyNote">完整追踪将查询、任务与结果发送至LangSmith。凭据在后端读取并脱敏；追踪服务异常不阻断Agent执行。</p>
      </Panel>
      <div className="settingsSide">
        <Panel title="运行数据库">
          <dl className="settingsList compactList"><div><dt>存储引擎</dt><dd>SQLite</dd></div><div><dt>持久化</dt><dd>{status.database?.persistent === true ? "已启用" : status.database?.persistent === false ? "未启用" : "未记录"}</dd></div><div><dt>已用容量</dt><dd>{formatBytes(status.database?.size_bytes)}</dd></div></dl>
          <label className="readonlyPath"><span>数据库路径</span><code>{status.database?.path ?? "未记录"}</code></label>
        </Panel>
        <Panel title="模型服务">
          <dl className="settingsList compactList"><div><dt>接入状态</dt><dd>{status.provider?.configured ? "配置就绪" : "待配置"}</dd></div><div><dt>Provider</dt><dd>{status.provider?.name ?? "未记录"}</dd></div><div><dt>模型</dt><dd>{status.provider?.model ?? "未记录"}</dd></div></dl>
          <p className="privacyNote">用量、成本与RequestID依据Provider返回值记录。</p>
        </Panel>
        <Panel title="LangGraphStudio">
          <p className="settingsIntro">独立调试工作流、节点状态与中断恢复。</p>
          <label className="readonlyPath studioCommand"><span>启动命令</span><code>.&#92;scripts&#92;start-studio.ps1</code></label>
          <p className="privacyNote">Studio使用独立线程，其执行记录不写入本站运行数据库。</p>
          <a className="secondaryButton inlineButton" href="https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2024" target="_blank" rel="noreferrer">打开Studio<ArrowSquareOut size={16} aria-hidden="true" /></a>
        </Panel>
      </div>
    </div>}
  </main>;
}
