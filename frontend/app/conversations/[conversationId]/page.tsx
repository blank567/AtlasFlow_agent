import { ConversationWorkspace } from "../../../components/conversation-workspace";

export default async function ConversationPage({ params }: { params: Promise<{ conversationId: string }> }) {
  const { conversationId } = await params;
  return <ConversationWorkspace key={conversationId} conversationId={conversationId} />;
}
