"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { listRuns } from "../lib/api";
import { RunRecord } from "../lib/types";
import { EmptyState, LoadingBlock, RunRow } from "./ui";

export function RecentRuns() {
  const [runs, setRuns] = useState<RunRecord[] | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const load = useCallback(() => {
    setRuns(null);
    setUnavailable(false);
    const params = new URLSearchParams({ page: "1", page_size: "4" });
    void listRuns(params).then((value) => setRuns(value.items)).catch(() => { setRuns([]); setUnavailable(true); });
  }, []);
  useEffect(load, [load]);
  if (runs === null) return <LoadingBlock label="读取最近运行" />;
  if (!runs.length) return <><EmptyState title={unavailable ? "运行记录读取失败" : "暂无运行记录"} description={unavailable ? "请检查后端连接后重试。" : "研究运行的执行状态与报告将在此归档。"} />{unavailable && <button type="button" className="secondaryButton" onClick={load}>重试</button>}</>;
  return <div className="runList compact">{runs.map((run) => <RunRow key={run.id} run={run} />)}<Link className="textLink" href="/runs">查看全部运行</Link></div>;
}
