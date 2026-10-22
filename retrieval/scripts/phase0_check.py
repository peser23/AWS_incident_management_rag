"""Phase 0 read-only access checks: Titan V2, AOSS index/pipeline, LLM."""
import json, os, sys
import boto3
from botocore.config import Config
from opensearchpy import AWSV4SignerAuth, OpenSearch, RequestsHttpConnection

REGION = os.getenv("AWS_REGION", "us-east-1")
ENDPOINT = os.getenv("DAIMO_OPENSEARCH_ENDPOINT", "https://sbcurg5zzjoxvd3g19x3.us-east-1.aoss.amazonaws.com")
INDEX = os.getenv("DAIMO_OPENSEARCH_INDEX", "daimo-incidents")
PIPELINE = os.getenv("HYBRID_PIPELINE_NAME", "daimo-hybrid-pipeline")
LLM = os.getenv("MODEL_ID", "amazon.nova-pro-v1:0")

def step(name, fn):
    try:
        print(f"[ OK ] {name}: {fn()}")
    except Exception as e:
        print(f"[FAIL] {name}: {type(e).__name__}: {str(e)[:300]}")

cfg = Config(region_name=REGION, retries={"max_attempts": 3, "mode": "adaptive"})
br = boto3.client("bedrock-runtime", config=cfg)
sess = boto3.Session()
os_client = OpenSearch(hosts=[ENDPOINT], http_auth=AWSV4SignerAuth(sess.get_credentials(), REGION, "aoss"),
                       use_ssl=True, verify_certs=True, connection_class=RequestsHttpConnection, timeout=10)

def titan():
    r = br.invoke_model(modelId="amazon.titan-embed-text-v2:0", body=json.dumps(
        {"inputText": "VistA login timeout", "dimensions": 1024, "normalize": True}))
    v = json.loads(r["body"].read())["embedding"]
    return f"dim={len(v)} norm={sum(x*x for x in v)**.5:.4f}"

def mapping():
    m = os_client.indices.get_mapping(index=INDEX)[INDEX]["mappings"]["properties"]
    return f"{len(m)} fields; embedding={m.get('embedding')}"

def count():
    return os_client.count(index=INDEX)["count"]

def pipeline():
    return json.dumps(os_client.transport.perform_request("GET", f"/_search/pipeline/{PIPELINE}"))[:200]

def llm():
    r = br.converse(modelId=LLM, messages=[{"role": "user", "content": [{"text": "Reply with: ok"}]}],
                    inferenceConfig={"maxTokens": 10})
    return r["output"]["message"]["content"][0]["text"]

step("Titan V2 embed", titan)
step("AOSS mapping", mapping)
step("AOSS doc count", count)
step("AOSS hybrid pipeline exists", pipeline)
step(f"LLM {LLM}", llm)
