import Link from "next/link";
import { KnowledgeWorkspace } from "../../components/knowledge-workspace";
import { PageHeader } from "../../components/ui";

export default function KnowledgePage() {
  return <main className="pageContent"><PageHeader title="知识库" description="管理原始资料、空间版本与发布索引。" actions={<><Link className="secondaryButton" href="/evaluations/rag">RAG实验</Link><Link className="primaryButton" href="/knowledge/lab">检索实验台</Link></>} /><KnowledgeWorkspace /></main>;
}
