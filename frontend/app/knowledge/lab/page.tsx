import Link from "next/link";
import { KnowledgeWorkspace } from "../../../components/knowledge-workspace";
import { PageHeader } from "../../../components/ui";

export default function KnowledgeLabPage() {
  return <main className="pageContent"><PageHeader title="检索实验台" description="选择知识空间，输入问题，再调整召回、融合和重排参数，观察哪些原文最终成为证据。" actions={<Link className="secondaryButton" href="/knowledge">返回文档</Link>} /><KnowledgeWorkspace view="lab" /></main>;
}
