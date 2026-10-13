import Link from "next/link";
import { KnowledgeWorkspace } from "../../../components/knowledge-workspace";
import { PageHeader } from "../../../components/ui";

export default function KnowledgeJobsPage() {
  return <main className="pageContent"><PageHeader title="处理任务" description="追踪文档从解析到发布的进度与失败原因。" actions={<Link className="secondaryButton" href="/knowledge">返回文档</Link>} /><KnowledgeWorkspace view="jobs" /></main>;
}
