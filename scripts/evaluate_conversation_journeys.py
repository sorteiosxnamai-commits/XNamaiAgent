"""Replay bounded, synthetic multi-turn journeys without sending WhatsApp.

ADMIN_API_TOKEN is read from the environment. No provider mutation is requested.
These checks complement human review; they are not a general quality score.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import statistics
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.commerce.catalog_filters import matches

FIXTURE = ROOT / "tests/evals/fixtures/mai_journeys.json"
FAILURES = {"factual_validation_failed", "ai_response_composition_failed", "consultative_unavailable",
            "tools_request_failed", "product_not_found", "order_lookup_failed"}


def evaluate_turn(case, result):
    reply = result.get("reply_text") or ""
    evaluation = result.get("evaluation") or {}
    failures = []
    if not reply.strip():
        failures.append("empty_reply")
    if result.get("handoff_required") or result.get("safety_reason") in FAILURES:
        failures.append("fallback:" + str(result.get("safety_reason")))
    for value in case.get("contains", []):
        if value.casefold() not in reply.casefold():
            failures.append("missing:" + value)
    for value in case.get("forbidden", []):
        if value.casefold() in reply.casefold():
            failures.append("forbidden:" + value)
    if case.get("no_commerce_lookup") and evaluation.get("used_commerce_provider"):
        failures.append("unnecessary_commerce_lookup")
    if case.get("sources") and evaluation.get("response_source") not in case["sources"]:
        failures.append("unexpected_route")
    if case.get("pending_action") and evaluation.get("pending_action") != case["pending_action"]:
        failures.append("missing_collection_state")
    if case.get("product_filter"):
        products = re.findall(r"^\s*\d+\.\s*(.+)$", reply, re.MULTILINE)
        if not products:
            failures.append("no_product_rows")
        elif any(not matches(case["product_filter"], {"name": product}) for product in products):
            failures.append("lost_product_filter")
    validation = evaluation.get("factual_validation") or {}
    if validation.get("valid") is False or validation.get("fallback_applied"):
        failures.append("invalid_claims")
    return failures


def run(base_url, token, journeys, send=None, on_turn=None):
    def request(body):
        req = urllib.request.Request(base_url.rstrip("/") + "/api/test/agent",
            data=json.dumps(body).encode(), headers={"Authorization": "Bearer " + token,
                                                    "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=90) as response:
            return json.load(response)
    send = send or request
    stamp = str(time.time_ns())[-8:]
    rows = []
    for index, journey in enumerate(journeys):
        history = []
        phone = "550" + stamp + f"{index:02d}"
        for number, case in enumerate(journey["turns"], 1):
            start = time.monotonic()
            try:
                response = send({"text":case["text"],"name":"Leonardo","phone":phone,"history":history[-24:]})
                failures = evaluate_turn(case, response)
                reply = response.get("reply_text") or ""
                history.extend([{"role":"user","content":case["text"]},{"role":"assistant","content":reply[:4000]}])
                row = {"scenario":journey["id"],"turn":number,"question":case["text"],"reply":reply,
                       "failures":failures,"evaluation":response.get("evaluation"),
                       "safety_reason":response.get("safety_reason"),"handoff_required":response.get("handoff_required")}
            except Exception as exc:
                row = {"scenario":journey["id"],"turn":number,"question":case["text"],
                       "failures":["request_failed:" + type(exc).__name__]}
            row["seconds"] = round(time.monotonic()-start,2)
            rows.append(row)
            if on_turn:
                on_turn(rows)
            print(json.dumps({k:row[k] for k in ("scenario","turn","failures","seconds")}),flush=True)
    return rows


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url",required=True)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--scenario",action="append")
    args=parser.parse_args()
    token=os.getenv("ADMIN_API_TOKEN")
    if not token:
        parser.error("ADMIN_API_TOKEN must be supplied through the environment")
    journeys=json.loads(FIXTURE.read_text(encoding="utf-8"))
    journeys=[j for j in journeys if not args.scenario or j["id"] in args.scenario]
    if not journeys:
        parser.error("no matching journeys")
    rows=run(args.base_url,token,journeys)
    report={"turns":len(rows),"failed_turns":sum(bool(r["failures"]) for r in rows),
            "median_seconds":statistics.median(r["seconds"] for r in rows),"results":rows}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    return 1 if report["failed_turns"] else 0


if __name__=="__main__":
    raise SystemExit(main())
