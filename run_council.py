#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import copilot_council as cc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GitHub Copilot-only LLM Council runner")
    parser.add_argument("command", choices=["catalog", "smoke", "ask"], help="operation to run")
    parser.add_argument("--config", default=str(SCRIPT_DIR.parent / "templates" / "council-config.json"))
    parser.add_argument("--question", help="question for ask mode")
    parser.add_argument("--model", action="append", help="optional model override(s) for smoke mode")
    return parser.parse_args()


def load_config(path: str) -> dict:
    cfg = cc.load_json(path)
    Path(cfg["artifact_root"]).mkdir(parents=True, exist_ok=True)
    return cfg


def cmd_catalog(cfg: dict) -> int:
    catalog = cc.fetch_model_catalog(timeout=min(60, int(cfg.get("request_timeout_seconds", 600))))
    catalog_map = cc.catalog_to_map(catalog)
    identity = cc.get_active_github_identity()
    print(f"active_github_login\t{identity.get('login')}")
    print(f"model_count\t{len(catalog_map)}")
    for model_id, item in sorted(catalog_map.items()):
        try:
            endpoint = cc.infer_endpoint(model_id, catalog_entry=item)
        except Exception:
            endpoint = "unknown"
        supports = (((item.get("capabilities") or {}).get("supports") or {}))
        flags = ",".join([k for k in ["vision", "tool_calls", "structured_outputs", "adaptive_thinking", "parallel_tool_calls"] if supports.get(k)])
        endpoints = ",".join(item.get("supported_endpoints") or [])
        print(f"{model_id}\tvendor={item.get('vendor')}\tendpoint={endpoint}\tsupported={endpoints}\tflags={flags}")
    return 0


def cmd_smoke(cfg: dict, models_override: list[str] | None) -> int:
    catalog = cc.fetch_model_catalog(timeout=min(60, int(cfg.get("request_timeout_seconds", 600))))
    catalog_map = cc.catalog_to_map(catalog)
    retry_policy = cc.get_retry_policy(cfg)
    if models_override:
        roster = [
            cc.CouncilMember(
                model=m,
                transport="copilot_api",
                endpoint=cc.infer_endpoint(m, catalog_entry=catalog_map.get(m)),
                role="outsider",
                requested_model=m,
                requested_transport="copilot_api",
                requested_endpoint=cc.infer_endpoint(m, catalog_entry=catalog_map.get(m)),
                resolution_reason="manual_override",
            )
            for m in models_override
        ]
    else:
        roster = cc.ensure_roster(cfg, catalog_map=catalog_map)
    rows = []
    failed = False
    timeout = min(60, cc.get_stage_timeout(cfg, "generation"))
    for member in roster:
        row = cc.smoke_one(member.model, member.transport, member.endpoint, timeout=timeout, retry_policy=retry_policy)
        rows.append(row)
        if not row["ok"]:
            failed = True
    print(json.dumps(rows, indent=2))
    return 1 if failed else 0


def cmd_ask(cfg: dict, question: str | None) -> int:
    if not question:
        raise SystemExit("--question is required for ask")
    result = cc.run_council(question, cfg)
    print(json.dumps({
        "artifacts": result["artifacts"],
        "ranking": result["stage2"]["aggregate"].get("ranking"),
        "final_answer": result["stage3"].get("final_answer"),
        "failures": result.get("failures"),
    }, indent=2, ensure_ascii=False))
    return 0


def main() -> int:
    args = parse_args()
    cfg = load_config(args.config)
    if args.command == "catalog":
        return cmd_catalog(cfg)
    if args.command == "smoke":
        return cmd_smoke(cfg, args.model)
    if args.command == "ask":
        return cmd_ask(cfg, args.question)
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
