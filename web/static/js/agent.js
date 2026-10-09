// The agents behind the Retrieval page: a LangGraph state machine over one
// runtime (the local one, or a Bedrock AgentCore Harness), read from /api/agent.

export function agentReady(info) {
  return Boolean(info?.configured && !info.error);
}
