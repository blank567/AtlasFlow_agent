"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { EvaluationArtifact, EvaluationDataset, EvaluationExperiment, EvaluationInput, EvaluationKind, EvaluationPlan, EvaluationPreflight, EvaluationResult, EvaluationSettings, evaluationApi, isActiveExperiment } from "../lib/evaluations-v2";

export function useEvaluationCatalog(kind: EvaluationKind) {
  const api = useMemo(() => evaluationApi(kind), [kind]);
  const [datasets, setDatasets] = useState<EvaluationDataset[]>([]), [settings, setSettings] = useState<EvaluationSettings | null>(null);
  const [inputs, setInputs] = useState<EvaluationInput[]>([]), [error, setError] = useState("");
  useEffect(() => { let disposed = false; Promise.all([api.datasets(), api.settings(), kind === "agent" ? api.inputs() : Promise.resolve([])]).then(([data, configuration, frozen]) => {
    if (!disposed) { setDatasets(data); setSettings(configuration); setInputs(frozen); }
  }).catch(reason => { if (!disposed) setError(reason instanceof Error ? reason.message : "实验目录读取失败"); }); return () => { disposed = true; }; }, [api, kind]);
  return { datasets, settings, inputs, setInputs, error };
}

export function useEvaluationSession(kind: EvaluationKind, plan: EvaluationPlan) {
  const api = useMemo(() => evaluationApi(kind), [kind]);
  const [experiments, setExperiments] = useState<EvaluationExperiment[]>([]), [selected, setSelected] = useState<EvaluationExperiment | null>(null);
  const [selectedCase, setSelectedCase] = useState<EvaluationResult | null>(null), [artifact, setArtifact] = useState<EvaluationArtifact | null>(null);
  const [artifactLoading, setArtifactLoading] = useState(false), [artifactError, setArtifactError] = useState("");
  const [preflight, setPreflight] = useState<{ hash: string; value: EvaluationPreflight } | null>(null);
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  const [notice, setNotice] = useState(""), [deletingId, setDeletingId] = useState<string | null>(null);
  const [confirmation, setConfirmation] = useState<{ action: "run" | "cancel" | "delete"; identity: string } | null>(null);
  const planHash = JSON.stringify(plan), currentPlanHash = useRef(planHash); currentPlanHash.current = planHash;
  const pending = useRef(false), selectionSequence = useRef(0), artifactSequence = useRef(0);
  const deletedIds = useRef(new Set<string>()), selectedRef = useRef(selected); selectedRef.current = selected;
  const checked = preflight?.hash === planHash ? preflight.value : null;
  useEffect(() => { let disposed = false; api.list().then(value => { if (!disposed) setExperiments(value.filter(item => !deletedIds.current.has(item.experiment_id))); }).catch(reason => { if (!disposed) setError(reason.message); }); return () => { disposed = true; }; }, [api]);
  useEffect(() => { if (confirmation?.action === "run" && confirmation.identity !== planHash) setConfirmation(null); }, [planHash, confirmation]);
  useEffect(() => { if (confirmation?.action === "cancel" && (confirmation.identity !== selected?.experiment_id || !isActiveExperiment(selected?.status))) setConfirmation(null); }, [selected?.experiment_id, selected?.status, confirmation]);
  useEffect(() => { if (confirmation?.action === "delete" && !experiments.some(item => item.experiment_id === confirmation.identity && !isActiveExperiment(item.status))) setConfirmation(null); }, [experiments, confirmation]);
  useEffect(() => {
    if (!selected || !isActiveExperiment(selected.status)) return;
    let disposed = false, fetching = false; const id = selected.experiment_id;
    const timer = window.setInterval(() => { if (fetching) return; fetching = true; api.get(id).then(value => { if (!disposed && !deletedIds.current.has(id)) { setSelected(value); setExperiments(previous => previous.map(item => item.experiment_id === id ? value : item)); } }).catch(reason => { if (!disposed && !deletedIds.current.has(id)) setError(reason.message); }).finally(() => { fetching = false; }); }, 2000);
    return () => { disposed = true; window.clearInterval(timer); };
  }, [api, selected?.experiment_id, selected?.status]);
  async function check() {
    if (pending.current) return; pending.current = true; setBusy(true); setError(""); const hash = planHash;
    try { const value = await api.preflight(plan); if (currentPlanHash.current === hash) setPreflight({ hash, value }); }
    catch (reason) { if (currentPlanHash.current === hash) setError(reason instanceof Error ? reason.message : "预检未完成"); }
    finally { pending.current = false; setBusy(false); }
  }
  async function confirm() {
    const action = confirmation; setConfirmation(null);
    if (!action || pending.current) return;
    if (action.action === "run" && (!checked?.ready || action.identity !== planHash)) return;
    if (action.action === "cancel" && (action.identity !== selected?.experiment_id || !isActiveExperiment(selected?.status))) return;
    if (action.action === "delete" && !experiments.some(item => item.experiment_id === action.identity && !isActiveExperiment(item.status))) return;
    pending.current = true; setBusy(true); setError(""); setNotice("");
    try {
      if (action.action === "delete") {
        setDeletingId(action.identity);
        await api.remove(action.identity);
        deletedIds.current.add(action.identity);
        setExperiments(previous => previous.filter(item => item.experiment_id !== action.identity));
        setSelected(current => current?.experiment_id === action.identity ? null : current);
        if (selectedRef.current?.experiment_id === action.identity) {
          selectedRef.current = null; closeDetail();
        }
        setNotice(`已删除实验记录${action.identity.slice(0, 8)}；原文、索引与证据保留。`);
        return;
      }
      const value = action.action === "run" ? await api.start({ ...plan, input_refs: plan.mode === "replay" ? { ...plan.input_refs, ...checked?.resolved_input_refs } : plan.input_refs }) : await api.cancel(action.identity);
      selectionSequence.current++; artifactSequence.current++; setSelected(value); setSelectedCase(null); setArtifact(null); setArtifactError("");
      setExperiments(previous => [value, ...previous.filter(item => item.experiment_id !== value.experiment_id)]);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "实验操作未完成"); }
    finally { pending.current = false; setBusy(false); setDeletingId(null); }
  }
  async function pick(id: string) {
    if (deletedIds.current.has(id)) return;
    const sequence = ++selectionSequence.current; artifactSequence.current++; setSelectedCase(null); setArtifact(null); setArtifactError(""); setError("");
    try { const value = await api.get(id); if (selectionSequence.current === sequence && !deletedIds.current.has(id)) setSelected(value); }
    catch (reason) { if (selectionSequence.current === sequence && !deletedIds.current.has(id)) setError(reason instanceof Error ? reason.message : "实验读取失败"); }
  }
  async function detail(result: EvaluationResult) {
    const sequence = ++artifactSequence.current; setSelectedCase(result); setArtifact(null); setArtifactError("");
    if (!selected || !result.input_id) { setArtifactLoading(false); return; }
    setArtifactLoading(true);
    try { const value = await api.artifact(selected.experiment_id, result.input_id); if (artifactSequence.current === sequence) setArtifact(value); }
    catch (reason) { if (artifactSequence.current === sequence) setArtifactError(reason instanceof Error ? reason.message : "冻结产物读取失败"); }
    finally { if (artifactSequence.current === sequence) setArtifactLoading(false); }
  }
  function closeDetail() {
    artifactSequence.current++;
    setSelectedCase(null); setArtifact(null); setArtifactLoading(false); setArtifactError("");
  }
  return { api, experiments, selected, selectedCase, artifact, artifactLoading, artifactError, busy, error, notice, deletingId, checked, check, pick, detail, closeDetail, confirmation, setConfirmation, confirm,
    askDelete: (id: string) => { if (!pending.current && experiments.some(item => item.experiment_id === id && !isActiveExperiment(item.status))) setConfirmation({ action: "delete", identity: id }); },
    askRun: () => setConfirmation({ action: "run", identity: planHash }), askCancel: () => selected && setConfirmation({ action: "cancel", identity: selected.experiment_id }) };
}

export type EvaluationSession = ReturnType<typeof useEvaluationSession>;
