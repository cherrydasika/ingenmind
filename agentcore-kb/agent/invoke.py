"""Send one question to the harness and stream the answer to the terminal.

    python agent/invoke.py "What is our refund policy?"

Reads HARNESS_ARN and AWS_REGION from the environment (or .env). The runtime
session id is derived from your username, so follow-up questions continue
the same conversation.
"""

import argparse
import getpass
import hashlib
import os
import re
import sys

import boto3
from botocore.exceptions import BotoCoreError, ClientError, EventStreamError
from dotenv import load_dotenv

load_dotenv()

ERROR_EVENTS = ("internalServerException", "validationException", "runtimeClientError")


def session_id() -> str:
    user = getpass.getuser()
    slug = re.sub(r"[^a-zA-Z0-9-_]", "-", user)[:40] or "user"
    return f"kb-{slug}-{hashlib.sha256(user.encode()).hexdigest()[:24]}"


def stream_answer(client, harness_arn: str, question: str) -> None:
    response = client.invoke_harness(
        harnessArn=harness_arn,
        runtimeSessionId=session_id(),
        messages=[{"role": "user", "content": [{"text": question}]}],
    )
    usage = None
    for event in response["stream"]:
        if "contentBlockStart" in event:
            tool = event["contentBlockStart"].get("start", {}).get("toolUse")
            if tool:
                print(f"\n[calling {tool.get('name', 'tool')}]", file=sys.stderr, flush=True)
        elif "contentBlockDelta" in event:
            text = event["contentBlockDelta"].get("delta", {}).get("text")
            if text:
                print(text, end="", flush=True)
        elif "messageStop" in event:
            if event["messageStop"].get("stopReason") == "end_turn":
                print()
        elif "metadata" in event:
            usage = event["metadata"].get("usage")
        else:
            error = next((event[name] for name in ERROR_EVENTS if name in event), None)
            if error is not None:
                raise RuntimeError(f"{next(n for n in ERROR_EVENTS if n in event)}: {error.get('message')}")
    if usage:
        print(f"\n[{usage.get('inputTokens')} tokens in, {usage.get('outputTokens')} out]", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description="Ask the knowledge-base agent a question.")
    parser.add_argument("question", help='e.g. "What is our refund policy?"')
    question = parser.parse_args().question.strip()
    harness_arn = os.environ.get("HARNESS_ARN", "").strip()
    region = os.environ.get("AWS_REGION", "").strip()
    if not harness_arn or not region:
        sys.exit("invoke: set HARNESS_ARN and AWS_REGION (run agent/deploy.py first)")
    if not question:
        sys.exit("invoke: the question is empty")
    client = boto3.client("bedrock-agentcore", region_name=region)
    try:
        stream_answer(client, harness_arn, question)
    except (ClientError, BotoCoreError, EventStreamError) as error:
        sys.exit(f"\ninvoke: {type(error).__name__}: {error}")
    except RuntimeError as error:
        sys.exit(f"\ninvoke: stream error: {error}")
    except KeyboardInterrupt:
        sys.exit("\ninvoke: interrupted")


if __name__ == "__main__":
    main()
