import Link from "next/link";
import { KnowledgeWorkspace } from "../../../components/knowledge-workspace";
import { PageHeader } from "../../../components/ui";

export default function KnowledgeLabPage() {
  return <main className="pageContent"><PageHeader title="检索实验台" description="配置召回策略与重排参数，核验检索证据。" actions={<Link className="secondaryButton" href="/knowledge">返回文档</Link>} /><KnowledgeWorkspace view="lab" /></main>;
}
