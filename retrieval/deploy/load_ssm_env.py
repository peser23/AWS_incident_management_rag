#!/opt/daimo-retrieval/venv/bin/python
"""ExecStartPre: read /daimo/retrieval/* from SSM Parameter Store into /run/daimo/retrieval.env (tmpfs).
Parameter /daimo/retrieval/MIN_CONFIDENCE becomes MIN_CONFIDENCE=...; config changes need only a restart."""
import os
import sys

import boto3

PREFIX = "/daimo/retrieval/"
OUT = os.getenv("DAIMO_ENV_OUT", "/run/daimo/retrieval.env")


def quote(v: str) -> str:
    if "'" in v or "\n" in v:
        raise ValueError("single quotes and newlines are not supported in parameter values")
    return f"'{v}'"  # systemd EnvironmentFile: single quotes are literal (JSON values stay intact)


def main() -> int:
    ssm = boto3.client("ssm", region_name=os.getenv("AWS_REGION", "us-east-1"))
    lines = []
    for page in ssm.get_paginator("get_parameters_by_path").paginate(Path=PREFIX, Recursive=False,
                                                                     WithDecryption=True):
        for p in page["Parameters"]:
            lines.append(f"{p['Name'][len(PREFIX):].upper()}={quote(p['Value'])}")
    fd = os.open(OUT, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(sorted(lines)) + ("\n" if lines else ""))
    os.chmod(OUT, 0o640)
    try:
        import grp
        os.chown(OUT, 0, grp.getgrnam("daimo").gr_gid)
    except (KeyError, PermissionError):
        pass
    print(f"loaded {len(lines)} parameters from {PREFIX}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
