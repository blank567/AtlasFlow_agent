import Link from "next/link";
import { RagEvaluationWorkspace } from "../../../components/evaluations/rag-workspace";
import { EvaluationNavigation } from "../../../components/evaluations/evaluation-common";
import { PageHeader } from "../../../components/ui";

export default function RagEvaluationPage() {
  return <main className="pageContent"><PageHeader title="RAG实验" description="固定语料与索引，核对证据定位、检索排名和回答依据。" actions={<Link className="secondaryButton" href="/knowledge/lab">单次检索</Link>} /><EvaluationNavigation active="rag" /><RagEvaluationWorkspace /></main>;
}
