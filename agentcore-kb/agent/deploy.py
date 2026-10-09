"""Create, or update, the Bedrock AgentCore Harness whose one tool is the
knowledge-base MCP server behind an AgentCore Gateway.

    python agent/deploy.py

Creates a new harness and prints its ARN; if HARNESS_ARN is set, updates
that harness instead. Reads from the environment (or .env):

    AWS_REGION          region of the harness, gateway and database (eu-west-2)
    HARNESS_ROLE_ARN    IAM role the harness assumes (Bedrock model access,
                        permission to call the gateway)
    GATEWAY_ARN         AgentCore Gateway whose target is the MCP server
    HARNESS_ARN         optional: update this harness instead of creating one
    HARNESS_NAME        optional, default agentcore_kb
    HARNESS_MODEL_ID    optional, default Claude Haiku 4.5 (EU inference profile)
"""

import os
import sys
import time
import uuid

import boto3
from botocore.exceptions import BotoCoreError, ClientError, WaiterError
from dotenv import load_dotenv

load_dotenv()

SYSTEM_PROMPT = (
    "You are a helpful assistant with access to a knowledge base. When a user asks a question, "
    "always search the knowledge base first before answering. Cite the source chunks you used "
    "in your response."
)
DEFAULT_MODEL_ID = "eu.anthropic.claude-haiku-4-5-20251001-v1:0"
TIMEOUT_SECONDS = 600


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"deploy: set {name} (see .env.example)")
    return value


def harness_config() -> dict:
    return {
        "executionRoleArn": required("HARNESS_ROLE_ARN"),
        "model": {"bedrockModelConfig": {
            "modelId": os.environ.get("HARNESS_MODEL_ID") or DEFAULT_MODEL_ID,
            "maxTokens": 2048,
            "temperature": 0.2,
        }},
        "systemPrompt": [{"text": SYSTEM_PROMPT}],
        "tools": [{
            "type": "agentcore_gateway",
            "name": "knowledge_base",
            "config": {"agentCoreGateway": {
                "gatewayArn": required("GATEWAY_ARN"),
                "outboundAuth": {"awsIam": {}},
            }},
        }],
        "maxIterations": 6,
        "timeoutSeconds": 120,
    }


def wait_until_ready(client, harness_id: str) -> dict:
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while True:
        harness = client.get_harness(harnessId=harness_id)["harness"]
        status = harness["status"]
        if status == "READY":
            return harness
        if status.endswith("_FAILED"):
            sys.exit(f"deploy: harness {status}: {harness.get('failureReason') or 'no reason given'}")
        if time.monotonic() > deadline:
            sys.exit(f"deploy: harness still {status} after {TIMEOUT_SECONDS}s")
        print(f"  status {status}…", flush=True)
        time.sleep(5)


def main() -> None:
    region = required("AWS_REGION")
    client = boto3.client("bedrock-agentcore-control", region_name=region)
    config = harness_config()
    existing = os.environ.get("HARNESS_ARN", "").strip()
    try:
        if existing:
            harness_id = existing.rsplit("/", 1)[-1]
            print(f"Updating harness {harness_id} in {region}")
            client.update_harness(harnessId=harness_id, clientToken=str(uuid.uuid4()), **config)
        else:
            name = os.environ.get("HARNESS_NAME") or "agentcore_kb"
            print(f"Creating harness {name} in {region}")
            harness_id = client.create_harness(harnessName=name, clientToken=str(uuid.uuid4()),
                                               **config)["harness"]["harnessId"]
        harness = wait_until_ready(client, harness_id)
    except (ClientError, BotoCoreError, WaiterError) as error:
        sys.exit(f"deploy: {type(error).__name__}: {error}")
    print(f"harnessArn: {harness['arn']}")
    if not existing:
        print("Add it to .env as HARNESS_ARN to update this harness next time.")


if __name__ == "__main__":
    main()
