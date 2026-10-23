"""S3 (or anything that speaks S3, such as MinIO) as a document connector (R48):
the files under a bucket's prefix. Credentials: the sealed access keys given
when it was added, else the server's own AWS credentials (its role on EC2)."""

from __future__ import annotations   # list[…] below, beside a method called list

import re
from datetime import datetime

from . import ConnectorError, Item

MAX_ITEMS = 1000


class S3Connector:
    def __init__(self, settings: dict, secret: dict):
        import boto3
        self.bucket, self.prefix = settings["bucket"], settings.get("prefix") or ""
        self.client = boto3.client(
            "s3", endpoint_url=settings.get("endpoint_url") or None, region_name=settings.get("region") or None,
            **({"aws_access_key_id": secret["access_key"], "aws_secret_access_key": secret["secret_key"]}
               if secret.get("access_key") else {}))

    @staticmethod
    def check(settings: dict) -> dict:
        bucket = str(settings.get("bucket") or "").strip()
        if not re.match(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", bucket):
            raise ConnectorError("a bucket name: 3–63 lower-case letters, digits, dots or hyphens")
        endpoint = str(settings.get("endpoint_url") or "").strip()
        if endpoint and not re.match(r"^https?://", endpoint):
            raise ConnectorError("the endpoint must be an http(s) URL (leave it empty for AWS S3)")
        return {"bucket": bucket, "prefix": str(settings.get("prefix") or "").strip().lstrip("/"),
                "endpoint_url": endpoint, "region": str(settings.get("region") or "").strip()}

    def source(self, item: Item) -> str:
        return f"s3://{self.bucket}/{item.id}"

    def list(self) -> list[Item]:
        items, token = [], None
        try:
            while len(items) < MAX_ITEMS:
                page = self.client.list_objects_v2(Bucket=self.bucket, Prefix=self.prefix,
                                                   **({"ContinuationToken": token} if token else {}))
                for obj in page.get("Contents") or []:
                    if not obj["Key"].endswith("/"):
                        items.append(Item(obj["Key"], obj["Key"].rsplit("/", 1)[-1], obj.get("Size", 0),
                                          obj["LastModified"].isoformat() if isinstance(obj.get("LastModified"), datetime)
                                          else None, (obj.get("ETag") or "").strip('"') or None))
                if not page.get("IsTruncated"):
                    break
                token = page.get("NextContinuationToken")
        except Exception as error:
            raise ConnectorError(f"can't list s3://{self.bucket}/{self.prefix}: {error}") from None
        return items[:MAX_ITEMS]

    def fetch(self, item_id: str) -> bytes:
        try:
            return self.client.get_object(Bucket=self.bucket, Key=item_id)["Body"].read()
        except Exception as error:
            raise ConnectorError(f"can't read s3://{self.bucket}/{item_id}: {error}") from None

    def changed_since(self, when: str) -> list[Item]:
        return [i for i in self.list() if i.modified and i.modified > when]
