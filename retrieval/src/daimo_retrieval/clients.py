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
def opensearch() -> OpenSearch:
    s = get_settings()
    auth = AWSV4SignerAuth(boto3.Session().get_credentials(), s.aws_region, "aoss")
    return OpenSearch(hosts=[s.daimo_opensearch_endpoint], http_auth=auth, use_ssl=True, verify_certs=True,
                      connection_class=RequestsHttpConnection, timeout=s.opensearch_timeout_s)
