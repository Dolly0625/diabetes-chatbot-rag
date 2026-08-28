from __future__ import annotations

import argparse
import json

from .graph import build_experimental_agent


DEFAULT_QUERY = "SGLT2 抑制劑有哪些 TFDA 藥品安全風險溝通資訊？"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the isolated TFDA MedRAX2-style experiment")
    parser.add_argument("query", nargs="?", default=DEFAULT_QUERY)
    parser.add_argument("--thread-id", default="learning-demo-thread")
    parser.add_argument("--json", action="store_true", help="Print the complete structured result")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    agent = build_experimental_agent()
    result = agent.run(args.query, thread_id=args.thread_id)
    if args.json:
        print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))
        return

    print("status: %s" % result.status)
    print("termination: %s" % result.termination_reason)
    print("tools: %s" % ", ".join(item.tool_name for item in result.tool_results))
    print("approved evidence: %s" % ", ".join(result.approved_evidence_ids))
    print()
    print(result.final_response)


if __name__ == "__main__":
    main()

