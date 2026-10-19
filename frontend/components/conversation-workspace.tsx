"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { FormEvent, useEffect, useRef, useState } from "react";
import { ArrowUp, ChatsCircle, FileText, Stop } from "@phosphor-icons/react";
import { RequestError, resolveApproval } from "../lib/api";
import { actOnConversationTurn, MessagePayload, sendConversationMessage, TURN_LABELS } from "../lib/conversations";
import { formatDate } from "../lib/format";
import { safeReportUrl } from "../lib/report-document";
import { RunRecord, TERMINAL_STATUSES } from "../lib/types";
import { useConversation } from "../hooks/use-conversation";
import { useRunStream } from "../hooks/use-run-stream";
import { ConfirmDialog } from "./confirm-dialog";
import { DeleteConversationButton } from "./delete-conversation-button";
import { ReportView } from "./report-view";
import { EmptyState, LoadingBlock, StatusBadge } from "./ui";

function MessageContent({ content }: { content: string }) {
  return <ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml urlTransform={(url) => safeReportUrl(url) ?? ""} components={{
    a: ({ href, children }) => href ? <a href={href} target="_blank" rel="noopener noreferrer">{children}</a> : <span>{children}</span>,
    img: ({ alt }) => <span>{alt || "图片未载入"}</span>,
    table: ({ children }) => <div className="conversationTable"><table>{children}</table></div>,
  }}>{content}</ReactMarkdown>;
}

function ActiveResearch({ initial, onChanged, disabled }: { initial: RunRecord; onChanged: () => unknown; disabled?: boolean }) {
  const { run, events, error, refresh } = useRunStream(initial.id);
  const current = run ?? initial;
  const [approving, setApproving] = useState(false);
  const [confirmApproval, setConfirmApproval] = useState(false);
  const [approvalError, setApprovalError] = useState("");
  const inFlight = useRef(false);
  useEffect(() => { if (disabled) setConfirmApproval(false); }, [disabled]);
  useEffect(() => {
    if (run && (TERMINAL_STATUSES.has(run.status) || run.status === "waiting_approval" || run.status === "interrupted")) void onChanged();
  }, [onChanged, run?.status, run?.id]);
  async function approve() {
    if (inFlight.current || disabled) return;
    inFlight.current = true; setApproving(true); setConfirmApproval(false); setApprovalError("");
    try { await resolveApproval(current.id, "approve"); await refresh(); await onChanged(); }
    catch (reason) { setApprovalError(reason instanceof Error ? reason.message : "审批失败"); }
    finally { setApproving(false); inFlight.current = false; }
  }
  return <section className="conversationRun" aria-label="本轮研究执行">
    <div className="conversationRunHeading"><strong>{current.execution_mode === "report_revision" ? "报告修订" : "研究执行"}</strong><StatusBadge status={current.status} /></div>
    <p role="status">{current.status === "waiting_approval" ? "研究计划已生成，核验后方可进入工具与研究阶段。" : current.status === "interrupted" ? "执行已中断。请在运行详情核验恢复条件，或停止本轮。" : events.at(-1)?.message || "已创建独立运行，正在准备执行。"}</p>
    {current.status === "waiting_approval" && <><ol className="conversationPlan">{current.plan?.tasks.map((task) => <li key={task.task_id}><strong>{task.title}</strong><span>{task.objective}</span></li>)}</ol><button type="button" className="primaryButton" disabled={approving || disabled} onClick={() => setConfirmApproval(true)}>{approving ? "提交审批…" : "批准计划"}</button></>}
    <Link href={`/runs/${current.id}`} className="textButton">{current.status === "waiting_approval" ? "查看或修改完整计划" : "查看拓扑、工具与执行详情"}</Link>
    {(error || approvalError) && <p className="formError" role="alert">{approvalError || error}</p>}
    {confirmApproval && <ConfirmDialog title="批准研究计划？" description="批准后将执行研究任务及必要工具，可能产生模型调用费用。" confirmLabel="批准并执行" onCancel={() => setConfirmApproval(false)} onConfirm={() => void approve()} />}
  </section>;
}

function ConversationReport({ runId }: { runId: string }) {
  const { run, error, refresh } = useRunStream(runId);
  if (error) return <div className="formError" role="alert">{error}<button className="textButton" onClick={() => void refresh()}>重试读取报告</button></div>;
  if (!run) return <LoadingBlock label="读取报告" />;
  return <><div className="conversationReportIdentity"><StatusBadge status={run.status} /><Link href={`/runs/${run.id}`} className="textButton">独立阅读与执行记录</Link></div><ReportView run={run} compact /></>;
}

export function ConversationWorkspace({ conversationId }: { conversationId: string }) {
  const router = useRouter();
  const { detail, error, connection, refresh, accept, deleted } = useConversation(conversationId);
  const [content, setContent] = useState("");
  const [replyBase, setReplyBase] = useState("");
  const [selectedReport, setSelectedReport] = useState(""); // Empty follows the latest report; old selection stays pinned.
  const [sending, setSending] = useState(false);
  const [actionBusy, setActionBusy] = useState(false);
  const [actionError, setActionError] = useState("");
  const [deleteBusy, setDeleteBusy] = useState(false);
  const deleteLock = useRef(false);
  const [pendingAction, setPendingAction] = useState<{ type: "confirm" | "cancel"; turnId: string; version: number } | null>(null);
  const pendingSend = useRef<MessagePayload | null>(null);
  const sendLock = useRef(false), actionLock = useRef(false);
  const transcript = useRef<HTMLDivElement>(null), composer = useRef<HTMLTextAreaElement>(null);
  const followBottom = useRef(true);
  const conversation = detail?.conversation;
  const activeTurn = detail?.turns.find((turn) => turn.id === conversation?.active_turn_id);
  const deleting = deleteBusy || !!conversation?.deleting || deleted;
  const blocked = deleting || !!activeTurn && activeTurn.status !== "awaiting_confirmation";
  const reports = detail?.reports.filter((report) => report.status !== "deleted") ?? [];
  const eligibleReports = reports.filter((report) => report.status === "completed" || report.status === "completed_with_warnings");
  const visibleReport = selectedReport || conversation?.current_report_run_id || "";
  const latestTurn = detail?.turns.at(-1);
  useEffect(() => { if (deleted) router.replace("/conversations"); }, [deleted, router]);
  useEffect(() => { if (deleting) setPendingAction(null); }, [deleting]);
  useEffect(() => {
    if (followBottom.current && transcript.current) transcript.current.scrollTop = transcript.current.scrollHeight;
  }, [detail?.messages.length, activeTurn?.status]);

  async function send(event?: FormEvent) {
    event?.preventDefault();
    if (!conversation || !content.trim() || content.length > 4000 || sendLock.current || deleteLock.current || blocked) return;
    sendLock.current = true; setSending(true); setActionError(""); followBottom.current = true;
    const text = content.trim();
    if (!pendingSend.current || pendingSend.current.content !== text || pendingSend.current.base_run_id !== (replyBase || null)) {
      pendingSend.current = { content: text, request_id: crypto.randomUUID(), expected_version: conversation.version, base_run_id: replyBase || null };
    }
    try {
      const value = await sendConversationMessage(conversation.id, pendingSend.current);
      accept(value); pendingSend.current = null; setContent("");
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : "发送失败，输入已保留。");
      // For an unknown network outcome, keep the exact payload/request ID for safe retry.
      if (reason instanceof RequestError && reason.status >= 400 && reason.status < 500) pendingSend.current = null;
      await refresh();
    } finally { setSending(false); sendLock.current = false; composer.current?.focus(); }
  }
  async function act() {
    if (!pendingAction || actionLock.current || deleteLock.current || deleting) return;
    const action = pendingAction; setPendingAction(null); actionLock.current = true; setActionBusy(true); setActionError("");
    try { accept(await actOnConversationTurn(conversationId, action.turnId, action.type, action.version)); }
    catch (reason) { setActionError(reason instanceof Error ? reason.message : "操作失败"); await refresh(); }
    finally { actionLock.current = false; setActionBusy(false); }
  }
  if (!detail) return <main className="pageContent"><Link href="/conversations" className="textButton">返回研究会话</Link>{error ? <div className="formError" role="alert">{error}<button className="textButton" onClick={() => window.location.reload()}>重新连接</button></div> : <LoadingBlock label="读取会话与报告" />}</main>;
  const trace = latestTurn?.trace_segments.findLast((segment) => !!safeReportUrl(segment.url ?? undefined));
  const sourceName = (runId: string) => { const index = detail.reports.findIndex((report) => report.run_id === runId); return index >= 0 ? `报告${index + 1}` : "来源报告"; };
  return <main className="pageContent conversationWorkspace">
    <header className="conversationHeader"><div><Link href="/conversations">研究会话</Link><h1>{detail.conversation.title}</h1></div><div className="conversationHeaderActions"><span className={`conversationConnection ${connection === "live" ? "is-live" : ""}`} role="status">{deleting ? "删除处理中" : connection === "live" ? "实时连接" : connection === "reconnecting" ? "连接恢复中" : "连接中"}</span><Link href="/conversations" className="secondaryButton">全部会话</Link><DeleteConversationButton conversation={detail.conversation} disabled={sending || actionBusy || deleteBusy} onBusyChange={(value) => { deleteLock.current = value; setDeleteBusy(value); if (value) setActionError(""); }} onDeleted={() => router.replace("/conversations")} onError={(message) => { setActionError(`${message}；删除未完成时可重试，已发生的执行停止不会回滚。`); void refresh(); }} /></div></header>
    {detail.conversation.deleting && <p className="conversationNote" role="status">此会话已进入删除流程，暂停新消息与执行。若上次清理中断，请点击“重试删除”完成清理。</p>}
    <section className="conversationMemory" aria-label="当前需求与执行范围">
      <div className="conversationMemoryHeading"><strong>当前需求</strong><span>{detail.conversation.policy.knowledge_mode === "off" ? "不使用知识库" : `知识空间：${detail.conversation.policy.knowledge_space || "默认空间"}`}</span></div>
      {Object.keys(detail.conversation.requirements).length ? <dl>{Object.entries(detail.conversation.requirements).map(([key, requirement]) => <div key={key}><dt>{key}</dt><dd>{requirement.value}<small>{requirement.confirmed ? "已确认" : "待确认"}</small></dd></div>)}</dl> : <p>明确表达的条件会在此保留；可通过后续消息修改，执行前再次确认。</p>}
    </section>
    <div className="conversationSplit">
      <section className="conversationDialogue" aria-label="研究对话">
        <div className="conversationDialogueHeading"><h2>对话</h2><span>{latestTurn ? TURN_LABELS[latestTurn.status] || latestTurn.status : "尚未开始"}</span></div>
        <div className="conversationTranscript" ref={transcript} onScroll={() => { const area = transcript.current; if (area) followBottom.current = area.scrollHeight - area.scrollTop - area.clientHeight < 120; }}>
          {!detail.messages.length && <div className="conversationWelcome"><ChatsCircle size={28} aria-hidden="true" /><h3>从研究问题开始</h3><p>先描述你要解决的问题。条件不足时先澄清，完成研究后可继续追问或修订报告。</p><button className="textButton" type="button" onClick={() => { setContent("帮我做一份旅游报告"); composer.current?.focus(); }}>试试：帮我做一份旅游报告</button></div>}
          {detail.messages.map((message) => {
            const turn = detail.turns.find((item) => item.id === message.turn_id);
            return <article key={message.id} id={`message-${message.id}`} className={`conversationMessage conversationMessage-${message.role} conversationMessage-${message.kind}`}>
              <header><strong>{message.role === "user" ? "你" : "AtlasFlow"}</strong><time dateTime={message.created_at}>{formatDate(message.created_at)}</time>{message.kind === "proposal" && turn?.status === "cancelled" && <span>已被替代或停止</span>}</header>
              <div className="conversationMessageBody"><MessageContent content={message.content} /></div>
              {message.run_id && <button className="conversationSource" type="button" onClick={() => setSelectedReport(message.run_id!)}><FileText size={14} aria-hidden="true" />{message.kind === "report" ? `阅读${sourceName(message.run_id)}` : `依据${sourceName(message.run_id)}回答`}</button>}
              {turn && turn.id === activeTurn?.id && message.kind === "proposal" && turn.status === "awaiting_confirmation" && <div className="conversationProposalActions"><button type="button" className="primaryButton" disabled={actionBusy || deleting} onClick={() => setPendingAction({ type: "confirm", turnId: turn.id, version: detail.conversation.version })}>确认执行</button><button type="button" className="textButton" disabled={actionBusy || deleting} onClick={() => composer.current?.focus()}>继续调整需求</button></div>}
            </article>;
          })}
          {activeTurn?.status === "routing" && <p className="conversationRouting" role="status"><span className="spinner" />理解本轮需求，判断是否需要新的研究…</p>}
          {detail.active_run && <ActiveResearch key={detail.active_run.id} initial={detail.active_run} onChanged={refresh} disabled={deleting} />}
        </div>
        {activeTurn && <div className="conversationActiveBar"><span>{TURN_LABELS[activeTurn.status]}</span><button type="button" className="textButton dangerText" disabled={actionBusy || deleting} onClick={() => setPendingAction({ type: "cancel", turnId: activeTurn.id, version: detail.conversation.version })}><Stop size={13} aria-hidden="true" />{activeTurn.status === "awaiting_confirmation" ? "放弃本轮建议" : "停止本轮"}</button></div>}
        {(actionError || error) && <p className="formError" role="alert">{actionError || error}</p>}
        <form className="conversationComposer" onSubmit={send} aria-busy={sending}>
          {eligibleReports.length > 0 && <label className="conversationReplyBase"><span>本轮依据</span><select aria-label="本轮依据报告" value={replyBase} disabled={sending || blocked} onChange={(event) => setReplyBase(event.target.value)}><option value="">按问题判断，默认当前报告</option>{eligibleReports.map((report) => <option key={report.run_id} value={report.run_id}>{sourceName(report.run_id)}：{report.title.slice(0, 35)}</option>)}</select></label>}
          <label className="srOnly" htmlFor="conversation-message">输入研究需求或报告追问</label><textarea ref={composer} id="conversation-message" value={content} onChange={(event) => setContent(event.target.value)} disabled={sending || blocked} maxLength={4000} rows={3} placeholder={blocked ? "本轮正在处理，请先完成审批或停止执行。" : "输入研究需求、补充条件，或追问已有报告…"} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void send(); } }} />
          <div className="conversationComposerFooter"><span>发送会调用主模型。研究与修订另行确认。<small>Enter发送，Shift+Enter换行</small></span><button type="submit" className="primaryButton" disabled={sending || blocked || !content.trim()} aria-label={sending ? "发送中" : "发送消息"}>{sending ? <span className="spinner" /> : <ArrowUp size={18} aria-hidden="true" />}<span>{sending ? "发送中" : "发送"}</span></button></div>
        </form>
        {trace?.url && <div className="conversationTrace"><a href={safeReportUrl(trace.url)} target="_blank" rel="noopener noreferrer">查看本轮LangSmith追踪</a><span>会话处理与研究执行分别记录</span></div>}
      </section>
      <section className="conversationReport" aria-label="报告工作区">
        <div className="conversationReportHeading"><div><h2>报告工作区</h2><p>保留每次研究与修订的产物，不覆盖历史报告。</p></div><label><span className="srOnly">阅读报告版本</span><select aria-label="阅读报告版本" value={selectedReport} onChange={(event) => setSelectedReport(event.target.value)}><option value="">跟随最新报告</option>{reports.map((report) => <option value={report.run_id} key={report.run_id}>{sourceName(report.run_id)}{report.status === "completed_with_warnings" ? "（含警告）" : report.status !== "completed" ? "（草稿）" : ""}</option>)}</select></label></div>
        {visibleReport ? <ConversationReport key={visibleReport} runId={visibleReport} /> : <EmptyState title="报告将在这里呈现" description="确认研究范围后生成首版报告；普通解释不会重新生成报告。" />}
      </section>
    </div>
    {pendingAction && <ConfirmDialog title={pendingAction.type === "confirm" ? "确认本轮执行范围？" : "停止本轮处理？"} description={pendingAction.type === "confirm" ? activeTurn?.action === "revise" ? "将依据来源报告调整结构与表达，并重新验收。不会调用研究工具，模型请求可能产生费用。" : "将按照当前完整需求创建独立研究，继承本会话的工具预算与知识空间。模型及工具请求可能产生费用。" : "输入、历史报告与已发生的事件会保留。已发送的远端请求仍可能计费，不会自动重跑。"} confirmLabel={pendingAction.type === "confirm" ? "确认并执行" : "停止本轮"} tone={pendingAction.type === "cancel" ? "danger" : "default"} onCancel={() => setPendingAction(null)} onConfirm={() => void act()} />}
  </main>;
}
