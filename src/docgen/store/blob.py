from __future__ import annotations

import io
from functools import lru_cache

import boto3
from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import ClientError

from docgen.config import get_settings


@lru_cache
def s3_client() -> BaseClient:
    s = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=s.s3_endpoint_url,
        aws_access_key_id=s.s3_access_key,
        aws_secret_access_key=s.s3_secret_key,
        region_name=s.s3_region,
        config=Config(
            signature_version="s3v4",
            retries={"max_attempts": 3, "mode": "standard"},
            # Without these, a slow S3 endpoint pins a request thread until the
            # OS-default socket timeout (minutes). Bound both so /render fails
            # fast rather than piling up in-flight requests.
            connect_timeout=5,
            read_timeout=30,
        ),
    )


# Buckets confirmed present (or just created) in this process. Populated by
# `ensure_bucket`; consulted before HeadBucket so put_object doesn't issue a
# per-write round-trip. Safe on the sync boto3 client — set-add is atomic on
# CPython and worst-case is a redundant HeadBucket on a race.
_bucket_confirmed: set[str] = set()


def ensure_bucket(bucket: str) -> None:
    if bucket in _bucket_confirmed:
        return
    client = s3_client()
    try:
        client.head_bucket(Bucket=bucket)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code in {"404", "NoSuchBucket", "NoSuchKey"}:
            client.create_bucket(Bucket=bucket)
        else:
            raise
    _bucket_confirmed.add(bucket)


def put_object(bucket: str, key: str, body: bytes, content_type: str = "application/octet-stream") -> str:
    ensure_bucket(bucket)
    s3_client().put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type)
    return key


def get_object(bucket: str, key: str) -> bytes:
    resp = s3_client().get_object(Bucket=bucket, Key=key)
    return resp["Body"].read()


def head_object(bucket: str, key: str) -> bool:
    """Return True if the object exists, False if it's gone. Reraises on
    unexpected errors (auth, network, etc.) so operational issues aren't
    silently classified as 'object missing'."""
    try:
        s3_client().head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise


def delete_object(bucket: str, key: str) -> None:
    """Best-effort delete; suppresses 'not found' since re-running is a no-op."""
    try:
        s3_client().delete_object(Bucket=bucket, Key=key)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code in {"404", "NoSuchKey", "NotFound"}:
            return
        raise


def stream_object(bucket: str, key: str) -> io.BytesIO:
    return io.BytesIO(get_object(bucket, key))
