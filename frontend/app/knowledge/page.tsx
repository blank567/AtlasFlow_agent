import Link from "next/link";
import { KnowledgeWorkspace } from "../../components/knowledge-workspace";
import { PageHeader } from "../../components/ui";

export default function KnowledgePage() {
  return <main className="pageContent"><PageHeader title="知识库" description="管理经过版本化、可追溯的知识来源。系统不会自动把项目文档或历史运行写入知识库。" actions={<><Link className="secondaryButton" href="/knowledge/jobs">处理任务</Link><Link className="primaryButton" href="/knowledge/lab">检索实验台</Link></>} /><KnowledgeWorkspace /></main>;
}
