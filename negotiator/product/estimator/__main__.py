from __future__ import annotations

import argparse
import json

from .documents import document_to_job_spec
from .voice import JobSpecStore, submit_job_spec


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay estimator inputs offline")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--doc")
    source.add_argument("--webhook", "--replay", dest="webhook")
    parser.add_argument("--confirmed", action="store_true", help="confirm a document read-back")
    parser.add_argument("--store", default=":memory:")
    args = parser.parse_args()
    if args.doc:
        spec = document_to_job_spec(args.doc, confirmed=True if args.confirmed else None)
        print(spec.model_dump_json(indent=2))
    else:
        with open(args.webhook, encoding="utf-8") as handle:
            payload = json.load(handle)
        print(json.dumps(submit_job_spec(payload, JobSpecStore(args.store)).as_response(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
