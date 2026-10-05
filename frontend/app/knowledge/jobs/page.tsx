import Link from "next/link";
import { KnowledgeWorkspace } from "../../../components/knowledge-workspace";
import { PageHeader } from "../../../components/ui";

export default function KnowledgeJobsPage() {
  return <main className="pageContent"><PageHeader title="处理任务" description="查看解析、切块、Embedding、索引和版本校验的真实状态。" actions={<Link className="secondaryButton" href="/knowledge">返回文档</Link>} /><KnowledgeWorkspace view="jobs" /></main>;
}
