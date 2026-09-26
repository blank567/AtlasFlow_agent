"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { listRuns } from "../lib/api";
import { RunRecord } from "../lib/types";
import { EmptyState, LoadingBlock, RunRow } from "./ui";

export function RecentRuns() {
  const [runs, setRuns] = useState<RunRecord[] | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  useEffect(() => {
    const params = new URLSearchParams({ page: "1", page_size: "4" });
    void listRuns(params).then((value) => setRuns(value.items)).catch(() => { setRuns([]); setUnavailable(true); });
  }, []);
  if (runs === null) return <LoadingBlock label="读取最近运行" />;
  if (!runs.length) return <EmptyState title={unavailable ? "运行列表接口暂不可用" : "还没有运行记录"} description={unavailable ? "后端升级完成后会自动显示持久化记录。" : "从上方创建第一个多 Agent 研究任务。"} />;
  return <div className="runList compact">{runs.map((run) => <RunRow key={run.id} run={run} />)}<Link className="textLink" href="/runs">查看全部运行</Link></div>;
}
