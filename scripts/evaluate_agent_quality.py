"""Compare two configured OpenAI roles remotely without sending customer messages.

Requires AGENT_ADMIN_URL and ADMIN_API_TOKEN in the process environment. Input is
an explicitly anonymized JSONL corpus. No OpenAI key is copied from Vercel.
"""
import argparse
import json
import os
from pathlib import Path
import statistics

import httpx


def summarize(reports):
    summary = {}
    for role in ("fast", "main"):
        rows = [r for report in reports for r in report.get("results", []) if r["role"] == role]
        good = [r for r in rows if "error_type" not in r]
        latencies = sorted(r["latency_ms"] for r in good)
        summary[role] = {"cases": len(rows), "errors": len(rows) - len(good),
            "assertions_passed": sum(r["score"]["passed_assertions"] for r in good),
            "latency_median_ms": statistics.median(latencies) if latencies else None,
            "input_tokens": sum(r.get("input_tokens") or 0 for r in good),
            "output_tokens": sum(r.get("output_tokens") or 0 for r in good)}
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", type=Path)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--tenant", default="xnamai")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=10, choices=range(1, 101))
    args = parser.parse_args()
    base = os.environ.get("AGENT_ADMIN_URL", "").rstrip("/")
    token = os.environ.get("ADMIN_API_TOKEN")
    if not base.startswith("https://") or not token:
        parser.error("AGENT_ADMIN_URL (https) and ADMIN_API_TOKEN are required")
    cases = [json.loads(line) for line in args.corpus.read_text(encoding="utf-8-sig").splitlines() if line.strip()][:args.limit]
    if not cases or any(case.get("anonymized") is not True for case in cases):
        parser.error("All cases must be anonymized and explicitly marked anonymized=true")
    reports = []
    with httpx.Client(timeout=50, follow_redirects=False) as client:
        for i, case in enumerate(cases):
            response = client.post(f"{base}/api/admin/agents/{args.tenant}/quality-evaluation",
                params={"workspace_id": args.workspace_id}, headers={"Authorization": f"Bearer {token}"},
                json={k: case[k] for k in ("question", "anonymized", "required", "forbidden") if k in case})
            if response.status_code != 200:
                raise SystemExit(f"Evaluation stopped: HTTP {response.status_code}, case {i + 1}. No automatic retry.")
            reports.append({"case_id": case.get("id", i + 1),
                            "origin": case.get("origin", "user_provided_anonymized"), **response.json()})
            args.output.write_text(json.dumps({"scope": "institutional_answer_composition",
                "human_review_required": True, "summary": summarize(reports), "cases": reports},
                ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved {len(reports)} comparisons to {args.output}")


if __name__ == "__main__":
    main()
