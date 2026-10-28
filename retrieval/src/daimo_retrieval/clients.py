"""Cached boto3 / opensearch-py clients, created once per worker."""
from functools import lru_cache

import boto3
from botocore.config import Config
from opensearchpy import AWSV4SignerAuth, OpenSearch, RequestsHttpConnection

from .settings import get_settings


@lru_cache
def bedrock_runtime():
    s = get_settings()
    cfg = Config(region_name=s.aws_region, retries={"max_attempts": 5, "mode": "adaptive"},
                 read_timeout=s.llm_timeout_s)
    return boto3.client("bedrock-runtime", config=cfg)


@lru_cache
def guard_bedrock_runtime():
    """Short timeout, one attempt: a slow classifier must fail open quickly instead of stalling the request."""
    s = get_settings()
    cfg = Config(region_name=s.aws_region, retries={"max_attempts": 1, "mode": "standard"},
                 connect_timeout=s.guard_timeout_s, read_timeout=s.guard_timeout_s)
    return boto3.client("bedrock-runtime", config=cfg)


@lru_cache
def opensearch() -> OpenSearch:
    s = get_settings()
    auth = AWSV4SignerAuth(boto3.Session().get_credentials(), s.aws_region, "aoss")
    return OpenSearch(hosts=[s.daimo_opensearch_endpoint], http_auth=auth, use_ssl=True, verify_certs=True,
                      connection_class=RequestsHttpConnection, timeout=s.opensearch_timeout_s)
