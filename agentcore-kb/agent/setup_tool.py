"""Create, or update, the knowledge-base tool the harness calls:

  1. AgentCore Runtime  agentcore_kb_mcp: the MCP server image, MCP protocol,
     VPC mode next to the database, IAM (SigV4) inbound auth
  2. AgentCore Gateway  agentcore-kb-gateway: MCP protocol, AWS_IAM authorizer
  3. Gateway target     knowledge-base: the runtime's MCP URL, called with the
     gateway's IAM role

    python agent/setup_tool.py --image-tag ff20141d9429

Safe to re-run: existing resources are found by name; the runtime is updated
when the image or its settings change. Network, roles and the ECR repository
come from Terraform (`terraform output -json agentcore_kb` in
infra/terraform), so the interface endpoints must be on
(scripts/aws/session.sh start --agent). Prints GATEWAY_ARN for .env.
"""

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import quote

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import load_dotenv

load_dotenv()

RUNTIME_NAME = "agentcore_kb_mcp"
GATEWAY_NAME = "agentcore-kb-gateway"
TARGET_NAME = "knowledge-base"
TERRAFORM_DIR = Path(__file__).resolve().parents[2] / "infra" / "terraform"
TIMEOUT_SECONDS = 900


def terraform_outputs() -> dict:
    try:
        raw = subprocess.run(["terraform", "output", "-json", "agentcore_kb"], cwd=TERRAFORM_DIR,
                             check=True, capture_output=True, text=True).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        sys.exit(f"setup: cannot read Terraform outputs in {TERRAFORM_DIR}: {error}")
    return json.loads(raw)


def interface_endpoints(region: str, subnet_id: str) -> int:
    """Live count of the agentcore-kb interface endpoints. The Terraform output
    can be stale: session.sh switches them with a targeted apply, which does
    not update outputs."""
    ec2 = boto3.client("ec2", region_name=region)
    vpc_id = ec2.describe_subnets(SubnetIds=[subnet_id])["Subnets"][0]["VpcId"]
    return len(ec2.describe_vpc_endpoints(Filters=[
        {"Name": "vpc-id", "Values": [vpc_id]},
        {"Name": "vpc-endpoint-type", "Values": ["Interface"]},
        {"Name": "tag:Name", "Values": ["agentcore-kb-*"]},
        {"Name": "vpc-endpoint-state", "Values": ["available"]},
    ])["VpcEndpoints"])


def wait_for(name: str, get_status, ready: str, failed: tuple[str, ...]):
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while True:
        status, reason = get_status()
        if status == ready:
            print(f"  {name}: {status}")
            return
        if status in failed:
            sys.exit(f"setup: {name} {status}: {reason or 'no reason given'}")
        if time.monotonic() > deadline:
            sys.exit(f"setup: {name} still {status} after {TIMEOUT_SECONDS}s")
        print(f"  {name}: {status}…", flush=True)
        time.sleep(10)


def runtime_config(tf: dict, image_tag: str, region: str) -> dict:
    return {
        "agentRuntimeArtifact": {"containerConfiguration": {
            "containerUri": f"{tf['ecr_repository_url']}:{image_tag}"}},
        "roleArn": tf["runtime_role_arn"],
        "networkConfiguration": {"networkMode": "VPC", "networkModeConfig": {
            "subnets": [tf["runtime_subnet_id"]], "securityGroups": [tf["runtime_sg_id"]]}},
        "protocolConfiguration": {"serverProtocol": "MCP"},
        "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 300, "maxLifetime": 3600},
        "environmentVariables": {
            "PGHOST": tf["pg_host"], "PGPORT": "5432", "PGDATABASE": "rag", "PGUSER": "kb_reader",
            "PGPASSWORD_PARAMETER": tf["pg_password_parameter"], "AWS_REGION": region,
        },
        "description": "agentcore-kb MCP server: search_knowledge_base over rag_chunks",
    }


def ensure_runtime(client, config: dict) -> str:
    existing = next((r for r in client.list_agent_runtimes()["agentRuntimes"]
                     if r["agentRuntimeName"] == RUNTIME_NAME), None)
    if existing:
        runtime_id = existing["agentRuntimeId"]
        current = client.get_agent_runtime(agentRuntimeId=runtime_id)
        if all(current.get(key) == value for key, value in config.items()):
            print(f"Runtime {runtime_id}: up to date")
        else:
            print(f"Runtime {runtime_id}: updating")
            client.update_agent_runtime(agentRuntimeId=runtime_id, clientToken=str(uuid.uuid4()), **config)
    else:
        print(f"Runtime {RUNTIME_NAME}: creating")
        runtime_id = client.create_agent_runtime(agentRuntimeName=RUNTIME_NAME, clientToken=str(uuid.uuid4()),
                                                 **config)["agentRuntimeId"]

    def status():
        runtime = client.get_agent_runtime(agentRuntimeId=runtime_id)
        return runtime["status"], runtime.get("failureReason")
    wait_for("runtime", status, "READY", ("CREATE_FAILED", "UPDATE_FAILED"))
    return client.get_agent_runtime(agentRuntimeId=runtime_id)["agentRuntimeArn"]


def ensure_gateway(client, tf: dict) -> dict:
    existing = next((g for g in client.list_gateways()["items"] if g["name"] == GATEWAY_NAME), None)
    if existing:
        gateway_id = existing["gatewayId"]
        print(f"Gateway {gateway_id}: exists")
    else:
        print(f"Gateway {GATEWAY_NAME}: creating")
        gateway_id = client.create_gateway(
            name=GATEWAY_NAME, clientToken=str(uuid.uuid4()), roleArn=tf["gateway_role_arn"],
            protocolType="MCP", authorizerType="AWS_IAM",
            description="agentcore-kb: the knowledge-base MCP server for the harness",
        )["gatewayId"]

    def status():
        gateway = client.get_gateway(gatewayIdentifier=gateway_id)
        return gateway["status"], "; ".join(gateway.get("statusReasons") or [])
    wait_for("gateway", status, "READY", ("FAILED", "UPDATE_UNSUCCESSFUL"))
    return client.get_gateway(gatewayIdentifier=gateway_id)


def ensure_target(client, gateway_id: str, runtime_arn: str, region: str) -> None:
    endpoint = (f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/"
                f"{quote(runtime_arn, safe='')}/invocations?qualifier=DEFAULT")
    config = {
        "targetConfiguration": {"mcp": {"mcpServer": {"endpoint": endpoint}}},
        "credentialProviderConfigurations": [{
            "credentialProviderType": "GATEWAY_IAM_ROLE",
            "credentialProvider": {"iamCredentialProvider": {"service": "bedrock-agentcore", "region": region}},
        }],
        "description": "search_knowledge_base on AgentCore Runtime",
    }
    existing = next((t for t in client.list_gateway_targets(gatewayIdentifier=gateway_id)["items"]
                     if t["name"] == TARGET_NAME), None)
    if existing:
        target_id = existing["targetId"]
        print(f"Target {target_id}: updating (re-lists the server's tools)")
        client.update_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id, name=TARGET_NAME, **config)
    else:
        print(f"Target {TARGET_NAME}: creating (the gateway lists the server's tools)")
        target_id = client.create_gateway_target(gatewayIdentifier=gateway_id, name=TARGET_NAME,
                                                 clientToken=str(uuid.uuid4()), **config)["targetId"]

    def status():
        target = client.get_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)
        return target["status"], "; ".join(target.get("statusReasons") or [])
    wait_for("target", status, "READY", ("FAILED", "UPDATE_UNSUCCESSFUL", "SYNCHRONIZE_UNSUCCESSFUL"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Create or update the knowledge-base runtime, gateway and target.")
    parser.add_argument("--image-tag", required=True, help="tag in the agentcore-kb-mcp ECR repository")
    args = parser.parse_args()
    region = os.environ.get("AWS_REGION", "eu-west-2")
    tf = terraform_outputs()
    if interface_endpoints(region, tf["runtime_subnet_id"]) < 5:
        sys.exit("setup: the interface endpoints are off; run scripts/aws/session.sh start --agent first")
    client = boto3.client("bedrock-agentcore-control", region_name=region)
    try:
        runtime_arn = ensure_runtime(client, runtime_config(tf, args.image_tag, region))
        gateway = ensure_gateway(client, tf)
        ensure_target(client, gateway["gatewayId"], runtime_arn, region)
    except (ClientError, BotoCoreError) as error:
        sys.exit(f"setup: {type(error).__name__}: {error}")
    print()
    print(f"RUNTIME_ARN={runtime_arn}")
    print(f"GATEWAY_ARN={gateway['gatewayArn']}")
    print(f"GATEWAY_URL={gateway['gatewayUrl']}")
    print(f"HARNESS_ROLE_ARN={tf['harness_role_arn']}")
    print("Add GATEWAY_ARN and HARNESS_ROLE_ARN to .env, then run agent/deploy.py.")


if __name__ == "__main__":
    main()
