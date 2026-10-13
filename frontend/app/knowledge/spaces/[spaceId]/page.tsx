import Link from "next/link";
import { KnowledgeWorkspace } from "../../../../components/knowledge-workspace";
import { PageHeader } from "../../../../components/ui";

export default async function KnowledgeSpacePage({ params }: { params: Promise<{ spaceId: string }> }) {
  const { spaceId } = await params;
  return <main className="pageContent"><PageHeader title="知识空间" description="管理这个空间的文档与发布版本。" actions={<Link className="secondaryButton" href="/knowledge">全部空间</Link>} /><KnowledgeWorkspace initialSpace={decodeURIComponent(spaceId)} /></main>;
}
