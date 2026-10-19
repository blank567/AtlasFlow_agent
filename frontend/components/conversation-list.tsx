"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { FormEvent, useEffect, useState } from "react";
import { ChatsCircle, Plus } from "@phosphor-icons/react";
import { listKnowledgeSpaces } from "../lib/api";
import { Conversation, createConversation, listConversations } from "../lib/conversations";
import { formatDate } from "../lib/format";
import { KnowledgeSpace } from "../lib/types";
import { EmptyState, LoadingBlock, PageHeader } from "./ui";
import { DeleteConversationButton } from "./delete-conversation-button";

export function ConversationList() {
  const router = useRouter();
  const [items, setItems] = useState<Conversation[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [createError, setCreateError] = useState("");
  const [busy, setBusy] = useState(false);
  const [title, setTitle] = useState("");
  const [spaces, setSpaces] = useState<KnowledgeSpace[]>([]);
  const [space, setSpace] = useState("");
  const [spaceError, setSpaceError] = useState("");
  const [autoApprove, setAutoApprove] = useState(false);
  const [agentLimit, setAgentLimit] = useState(10);
  const [runLimit, setRunLimit] = useState(40);
  const [reload, setReload] = useState(0);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [deleteError, setDeleteError] = useState("");
  const [deleteNotice, setDeleteNotice] = useState("");
  useEffect(() => {
    let active = true;
    setLoading(true); setError("");
    listConversations(page).then((value) => { if (active) { setItems(value.items); setTotal(value.total); } })
      .catch((reason) => { if (active) setError(reason instanceof Error ? reason.message : "会话列表读取失败"); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [page, reload]);
  useEffect(() => {
    let active = true;
    listKnowledgeSpaces().then((value) => { if (active) setSpaces(value); })
      .catch(() => { if (active) setSpaceError("知识空间暂不可用，仍可创建不使用知识库的会话。"); });
    return () => { active = false; };
  }, []);
  async function create(event: FormEvent) {
    event.preventDefault(); if (busy) return;
    setCreateError("");
    if (!Number.isInteger(agentLimit) || !Number.isInteger(runLimit) || agentLimit < 1 || agentLimit > 10 || runLimit < agentLimit || runLimit > 50) {
      setCreateError("单次Agent工具上限为1—10次，运行总额为1—50次且不得低于单次上限。"); return;
    }
    setBusy(true);
    try {
      const conversation = await createConversation({ title: title.trim() || "新会话", auto_approve: autoApprove,
        policy: { knowledge_mode: space ? "selected" : "off", knowledge_space: space || null,
          max_tool_calls_per_turn: agentLimit, max_tool_calls_per_run: runLimit } });
      router.push(`/conversations/${conversation.id}`);
    } catch (reason) { setCreateError(reason instanceof Error ? reason.message : "创建失败"); setBusy(false); }
  }
  return <main className="pageContent conversationIndex">
    <PageHeader title="研究会话" description="在连续对话中明确需求、追问证据并迭代报告。每次研究独立留痕。" />
    <div className="conversationIndexLayout">
      <section className="conversationHistory" aria-label="已有会话">
        <div className="conversationSectionHead"><h2>最近会话</h2><span>{total}个会话</span></div>
        {deleteError && <p role="alert" className="formError">{deleteError}</p>}
        {deleteNotice && <p role="status" className="conversationNote">{deleteNotice}</p>}
        {loading ? <LoadingBlock label="读取会话" /> : error ? <div role="alert" className="formError">{error}<button type="button" className="textButton" onClick={() => setReload((value) => value + 1)}>重试读取</button></div> : items.length ? <div className="conversationRows">{items.map((item) => <article className="conversationRowItem" key={item.id}><Link href={`/conversations/${item.id}`} className="conversationRow">
          <ChatsCircle size={21} aria-hidden="true" /><div><strong>{item.title}</strong><p>{Object.entries(item.requirements).slice(0, 3).map(([key, value]) => `${key}：${value.value}`).join("；") || "需求与报告保存在本地，可随时继续。"}</p><time dateTime={item.updated_at}>{formatDate(item.updated_at)}</time></div><span>{item.deleting ? "删除待完成" : item.active_turn_id ? "处理中" : "继续对话"}</span>
        </Link><DeleteConversationButton conversation={item} disabled={!!deletingId} onBusyChange={(value) => { setDeletingId(value ? item.id : null); if (value) { setDeleteError(""); setDeleteNotice(""); } }} onError={(message) => { setDeleteError(`${message}；请核对后重试删除。`); setReload((value) => value + 1); }} onDeleted={(result) => {
          setItems((values) => values.filter((value) => value.id !== result.conversation_id));
          const remaining = Math.max(0, total - 1); setTotal(remaining);
          setDeleteNotice(result.deleted_run_count === null ? "会话已删除。" : `会话已删除，本次清理${result.deleted_run_count}个关联Run。`);
          const nextPage = Math.max(1, Math.min(page, Math.ceil(remaining / 30))); setPage(nextPage);
          setReload((value) => value + 1);
        }} /></article>)}</div> : <EmptyState title="尚无研究会话" description="创建一个会话，从研究问题开始；条件不完整时，系统会先与你澄清。" />}
        {total > 30 && <nav className="conversationPagination" aria-label="会话分页"><button className="secondaryButton" disabled={page <= 1} onClick={() => setPage(page - 1)}>上一页</button><span>{page}/{Math.ceil(total / 30)}</span><button className="secondaryButton" disabled={page * 30 >= total} onClick={() => setPage(page + 1)}>下一页</button></nav>}
      </section>
      <form className="conversationCreate" onSubmit={create} aria-busy={busy}>
        <h2>新建会话</h2><p>配置对本会话中的研究生效，发送消息后再确认具体执行范围。</p>
        <label className="field"><span>会话名称</span><input value={title} maxLength={100} disabled={busy} onChange={(event) => setTitle(event.target.value)} placeholder="可留空，按首条消息命名" /></label>
        <label className="field"><span>知识检索空间</span><select value={space} disabled={busy} onChange={(event) => setSpace(event.target.value)}><option value="">不使用知识库</option>{spaces.map((item) => <option value={item.slug} key={item.id}>{item.name}·{item.slug}</option>)}</select><small>会话内固定使用该空间，不自动切换。</small></label>
        <div className="conversationBudget"><label className="field"><span>单次Agent工具上限</span><input type="number" min={1} max={10} required disabled={busy} value={agentLimit} onChange={(event) => setAgentLimit(Number(event.target.value))} /></label><label className="field"><span>每次研究工具总额</span><input type="number" min={1} max={50} required disabled={busy} value={runLimit} onChange={(event) => setRunLimit(Number(event.target.value))} /></label></div>
        <label className="conversationAutoApprove"><input type="checkbox" disabled={busy} checked={autoApprove} onChange={(event) => setAutoApprove(event.target.checked)} /><span>计划生成后自动审批<small>默认关闭，研究计划需人工核验。</small></span></label>
        {spaceError && <p className="conversationNote" role="status">{spaceError}</p>}
        {createError && <p className="formError" role="alert">{createError}</p>}
        <button className="primaryButton" disabled={busy}><Plus size={17} aria-hidden="true" />{busy ? "创建中…" : "创建会话"}</button>
        <small className="conversationNote">创建会话不调用模型。会话与报告保存在E盘现有业务数据库。</small>
      </form>
    </div>
  </main>;
}
