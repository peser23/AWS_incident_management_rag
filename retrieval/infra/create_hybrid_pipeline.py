"""Create the AOSS hybrid search pipeline. Run with an ADMIN/deploy identity (needs aoss:CreateCollectionItems,
aoss:UpdateCollectionItems); the runtime role must not have these. Idempotent (PUT)."""
import os, boto3
from opensearchpy import AWSV4SignerAuth, OpenSearch, RequestsHttpConnection

REGION = os.getenv("AWS_REGION", "us-east-1")
ENDPOINT = os.getenv("DAIMO_OPENSEARCH_ENDPOINT", "https://sbcurg5zzjoxvd3g19x3.us-east-1.aoss.amazonaws.com")
NAME = os.getenv("HYBRID_PIPELINE_NAME", "daimo-hybrid-pipeline")
WEIGHTS = [float(x) for x in os.getenv("HYBRID_WEIGHTS", "0.3,0.7").split(",")]

body = {
    "description": "DAIMO hybrid BM25 + kNN",
    "phase_results_processors": [{"normalization-processor": {
        "normalization": {"technique": "min_max"},
        "combination": {"technique": "arithmetic_mean", "parameters": {"weights": WEIGHTS}}}}],
}
c = OpenSearch(hosts=[ENDPOINT], http_auth=AWSV4SignerAuth(boto3.Session().get_credentials(), REGION, "aoss"),
               use_ssl=True, verify_certs=True, connection_class=RequestsHttpConnection, timeout=30)
print(c.transport.perform_request("PUT", f"/_search/pipeline/{NAME}", body=body))
print("Note: new pipelines can take up to 15 s to become searchable.")
