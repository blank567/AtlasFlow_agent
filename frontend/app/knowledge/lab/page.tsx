import Link from "next/link";
import { KnowledgeWorkspace } from "../../../components/knowledge-workspace";
import { PageHeader } from "../../../components/ui";

export default function KnowledgeLabPage() {
  return <main className="pageContent"><PageHeader title="检索实验台" description="沿着排名轨迹检查词法召回、向量召回、RRF 融合和 Rerank，判断证据为什么被选中。" actions={<Link className="secondaryButton" href="/knowledge">返回文档</Link>} /><KnowledgeWorkspace view="lab" /></main>;
}
