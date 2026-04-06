#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / 'scripts' / 'run_council.py'
DEFAULT_CONFIG = ROOT / 'templates' / 'council-config.json'
DEFAULT_OUTDIR = ROOT / 'assets' / 'runs' / 'pressure-tests'

QUESTIONS = [
    "What is the strongest reason to keep the council's three personas fixed rather than user-configurable? Answer concisely.",
    "What is the strongest argument against keeping the council's three personas fixed rather than user-configurable? Answer concisely.",
    "What is the minimum acceptable evidence that review JSON salvage is helping instead of hiding model failures?",
    "How should the council report degraded-review confidence without becoming too verbose?",
    "What is the most dangerous failure mode of a 3x3 model-by-persona council in production?",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def run_once(config: Path, question: str, timeout: int) -> dict[str, Any]:
    cmd = [sys.executable, str(RUNNER), 'ask', '--config', str(config), '--question', question]
    start = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    elapsed_ms = int((time.time() - start) * 1000)
    row: dict[str, Any] = {
        'question': question,
        'elapsed_ms': elapsed_ms,
        'exit_code': proc.returncode,
        'stdout': proc.stdout,
        'stderr': proc.stderr,
        'ok': proc.returncode == 0,
    }
    if proc.returncode != 0:
        return row
    payload = json.loads(proc.stdout)
    result_path = Path(payload['artifacts']['result_json'])
    result = json.loads(result_path.read_text())
    stage1 = result.get('stage1', {})
    stage2 = result.get('stage2', {})
    aggregate = stage2.get('aggregate', {})
    row.update({
        'result_json': str(result_path),
        'summary_md': payload['artifacts']['summary_md'],
        'run_dir': payload['artifacts']['run_dir'],
        'successful_seats': len(stage1.get('candidates', [])),
        'stage1_failures': len(stage1.get('failures', [])),
        'reviews': len(stage2.get('reviews', [])),
        'review_failures': len(stage2.get('review_failures', [])),
        'review_substitutions': len(stage2.get('review_substitutions', [])),
        'failures': len(result.get('failures', [])),
        'ranking_len': len(aggregate.get('ranking') or []),
        'summary_config_present': 'summary_config' in result,
        'compact_summary_present': 'compact_summary' in result,
        'chairman_ok': bool(result.get('stage3', {}).get('ok')),
    })
    return row


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ok_rows = [r for r in rows if r.get('ok')]
    return {
        'total_runs': len(rows),
        'ok_runs': len(ok_rows),
        'failed_runs': len(rows) - len(ok_rows),
        'zero_failure_runs': sum(1 for r in ok_rows if r.get('failures', 1) == 0),
        'full_review_runs': sum(1 for r in ok_rows if r.get('reviews') == 9 and r.get('review_failures') == 0),
        'compact_summary_runs': sum(1 for r in ok_rows if r.get('compact_summary_present')),
        'summary_config_runs': sum(1 for r in ok_rows if r.get('summary_config_present')),
        'max_elapsed_ms': max((r.get('elapsed_ms', 0) for r in rows), default=0),
        'avg_elapsed_ms': int(sum(r.get('elapsed_ms', 0) for r in rows) / len(rows)) if rows else 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description='Pressure-test copilot-llm-council with repeated live runs')
    parser.add_argument('--config', default=str(DEFAULT_CONFIG))
    parser.add_argument('--count', type=int, default=20)
    parser.add_argument('--timeout', type=int, default=1200)
    parser.add_argument('--outdir', default=str(DEFAULT_OUTDIR))
    args = parser.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '--pressure-test'
    run_dir = outdir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    for idx in range(args.count):
        question = f'Pressure test run {idx+1}/{args.count}. ' + QUESTIONS[idx % len(QUESTIONS)]
        row = run_once(Path(args.config), question, args.timeout)
        row['run_index'] = idx + 1
        row['timestamp'] = utc_now()
        rows.append(row)
        (run_dir / 'rows.json').write_text(json.dumps(rows, indent=2, ensure_ascii=False) + '\n')
        (run_dir / 'summary.json').write_text(json.dumps(summarize(rows), indent=2, ensure_ascii=False) + '\n')
        print(json.dumps({'run_index': idx + 1, 'ok': row['ok'], 'reviews': row.get('reviews'), 'review_failures': row.get('review_failures'), 'failures': row.get('failures')}, ensure_ascii=False), flush=True)

    final = summarize(rows)
    final['run_dir'] = str(run_dir)
    print(json.dumps(final, indent=2, ensure_ascii=False))
    return 0 if final['failed_runs'] == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
