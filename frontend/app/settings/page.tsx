"use client";

import { useEffect, useState } from "react";
import { checkLangSmith, getSettingsStatus } from "../../lib/api";
import { formatBytes, formatDate } from "../../lib/format";
import { SettingsStatus } from "../../lib/types";
import { LoadingBlock, PageHeader, Panel } from "../../components/ui";

const CONFIG_TEMPLATE = `# LangSmith（仅后端读取，不要使用 NEXT_PUBLIC_ 前缀）
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=lsv2_...
LANGSMITH_PROJECT=atlasflow
LANGSMITH_ENDPOINT=https://api.smith.langchain.com
LANGSMITH_TRACE_CONTENT=true`;

const STATE_LABEL: Record<string, string> = { ready: "连接正常", disabled: "追踪未启用", not_configured: "尚未配置", unreachable: "连接失败", error: "连接失败" };

export default function SettingsPage() {
  const [status, setStatus] = useState<SettingsStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);
  const [copied, setCopied] = useState(false);

  async function load() {
    try { setStatus(await getSettingsStatus()); setError(null); } catch (reason) { setError(reason instanceof Error ? reason.message : "配置状态读取失败"); }
  }
  useEffect(() => { void load(); }, []);

  async function check() {
    setChecking(true); setError(null);
    try {
      const result = await checkLangSmith();
      if (result && "langsmith" in result) setStatus(result as SettingsStatus);
      else setStatus((current) => ({ ...current, langsmith: result as SettingsStatus["langsmith"] }));
    } catch (reason) { setError(reason instanceof Error ? reason.message : "LangSmith 检测失败"); }
    finally { setChecking(false); }
  }

  async function copyTemplate() {
    await navigator.clipboard.writeText(CONFIG_TEMPLATE);
    setCopied(true); window.setTimeout(() => setCopied(false), 1800);
  }

  const langsmith = status?.langsmith;
  const state = langsmith?.state ?? (langsmith?.configured ? langsmith.enabled ? "ready" : "disabled" : "not_configured");
  const stateLabel = state === "ready" && langsmith?.connection_status !== "reachable" ? "已配置 · 待检测" : STATE_LABEL[state] ?? state;
  return (
    <main className="pageContent">
      <PageHeader eyebrow="连接与存储" title="让运行环境保持透明" description="检查追踪、模型和本地数据库状态；密钥不会传到浏览器。" actions={<span className="softBadge">v{status?.app_version ?? "0.6.0"}</span>} />
      {error && <div className="inlineAlert errorAlert"><strong>检测未完成</strong><span>{error}</span></div>}
      {!status && !error ? <LoadingBlock /> : <div className="settingsGrid">
        <Panel title="LangSmith 追踪" meta={<span className={`connectionBadge connection-${state}`}><i />{stateLabel}</span>} className="settingsMain">
          <p className="settingsIntro">一个 AtlasFlow Run 可以对应多个 Trace Segment，并通过 <code>atlasflow_run_id</code> 关联。LangSmith 不可用时，Agent 主流程继续执行。</p>
          <dl className="settingsList">
            <div><dt>Tracing</dt><dd>{langsmith?.enabled ? "已启用" : "未启用"}</dd></div>
            <div><dt>API Key</dt><dd>{langsmith?.configured ? "已配置（已隐藏）" : "未配置"}</dd></div>
            <div><dt>Project</dt><dd>{langsmith?.project ?? "不可用"}</dd></div>
            <div><dt>Endpoint</dt><dd>{langsmith?.endpoint ?? "不可用"}</dd></div>
            <div><dt>Trace 内容</dt><dd>{langsmith?.trace_content === true || langsmith?.trace_content === "full" ? "允许完整内容" : "默认脱敏元数据"}</dd></div>
            <div><dt>最近检测</dt><dd>{formatDate(langsmith?.last_checked_at)}</dd></div>
          </dl>
          {langsmith?.message && <div className="inlineAlert neutralAlert"><span>{langsmith.message}</span></div>}
          <p className="privacyNote">开启 Trace 内容后，新运行的查询、任务和结果会发送到 LangSmith；密钥字段仍会脱敏。旧 Trace 的空白输入输出不会补录。</p>
          <div className="settingsActions"><button className="primaryButton" disabled={checking} onClick={() => void check()}>{checking ? <><span className="spinner" />检测中</> : "重新检测 LangSmith"}</button><button className="secondaryButton" onClick={() => void copyTemplate()}>{copied ? "已复制 ✓" : "复制 .env 模板"}</button></div>
        </Panel>
        <div className="settingsSide">
          <Panel title="运行数据库">
            <dl className="settingsList compactList"><div><dt>类型</dt><dd>SQLite</dd></div><div><dt>持久化</dt><dd>{status?.database?.persistent === false ? "否" : "是"}</dd></div><div><dt>占用空间</dt><dd>{formatBytes(status?.database?.size_bytes)}</dd></div></dl>
            <label className="readonlyPath"><span>数据库位置</span><code>{status?.database?.path ?? "不可用"}</code></label>
          </Panel>
          <Panel title="模型 Provider">
            <dl className="settingsList compactList"><div><dt>状态</dt><dd>{status?.provider?.configured ? "已配置" : "未配置"}</dd></div><div><dt>Provider</dt><dd>{status?.provider?.name ?? "OpenRouter"}</dd></div><div><dt>Model</dt><dd>{status?.provider?.model ?? "不可用"}</dd></div></dl>
            <p className="privacyNote">Token、成本和 Request ID 仅在 Provider 返回真实 usage 时展示，不进行估算。</p>
          </Panel>
          <Panel title="LangGraph Studio">
            <p className="privacyNote">先在 E 盘终端运行 <code>.\scripts\start-studio.ps1</code>，再打开本地 Studio。Studio 使用独立线程，不会写入 AtlasFlow 的 Run/Event 数据库。</p>
            <a className="secondaryButton inlineButton" href="https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2024" target="_blank" rel="noreferrer">打开 Studio</a>
          </Panel>
        </div>
      </div>}
    </main>
  );
}
