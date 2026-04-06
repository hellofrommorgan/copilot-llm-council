# Copilot LLM Council: concrete spec

## Goal

Port the core llm-council protocol into Hermes as a reusable skill and CLI runner that uses GitHub Copilot OAuth only.

The deliverable is not a web app. It is:
- a Hermes skill that explains the workflow
- a Python council engine with no external runtime dependencies
- a CLI entrypoint for catalog probe, smoke tests, and end-to-end council runs
- durable JSON and Markdown artifacts for every real run

## Why this exists

Karpathy's `llm-council` has the right interaction pattern:
1. first opinions
2. anonymized review
3. chairman synthesis

But his implementation assumes OpenRouter and a frontend/backend app. For Hermes, the right translation is a repo-like operator workflow:
- catalog probe
- model smoke validation
- council run
- artifact capture
- human-readable summary

## Scope

### In scope
- GitHub token resolution from environment or `gh auth token`
- Copilot model catalog probe via `https://api.githubcopilot.com/models`
- Endpoint inference per model
- Stage 1 generation across a roster of models
- Stage 2 review in either `peer` or `judge` mode
- Stage 3 chairman synthesis
- JSON and Markdown artifact writing
- Unit tests for parsing, endpoint inference, and review aggregation
- Real smoke and real hard-question runs

### Out of scope for v1
- Web UI
- Streaming UI
- Tool-calling inside council members
- Retrieval augmentation
- Auto-optimization of roster by benchmark history
- Full `/v1/messages` adapter if `/chat/completions` works for Claude-family defaults

## Product shape

The council runner behaves like a small CLI product with three commands:

1. `catalog`
   - fetch active Copilot model catalog
   - print active GitHub login and model count
   - print one TSV-like row per model with vendor, inferred endpoint, supported endpoints, and capability flags

2. `smoke`
   - run a tiny exact-match prompt through each configured model or explicit `--model` override
   - verify endpoint routing and response parsing
   - print a JSON array of per-model rows including `model`, `transport`, `endpoint`, `ok`, `text`, `latency_ms`, `attempt_count`, and `attempts`

3. `ask`
   - run the full council protocol on a question
   - write artifacts
   - print a compact JSON envelope with `artifacts`, `ranking`, `final_answer`, and `failures`

All commands accept `--config`. `ask` requires `--question`.

## File layout

Skill directory:
- `SKILL.md`
- `references/spec.md`
- `references/model-matrix.md`
- `references/prompt-templates.md`
- `templates/council-config.json`
- `scripts/copilot_council.py`
- `scripts/run_council.py`
- `scripts/tests/test_copilot_council.py`
- runtime artifacts under `assets/runs/`

## Config schema

Preferred JSON file shape:

```json
{
  "artifact_root": "/absolute/path/to/assets/runs",
  "mode": "peer",
  "chairman": {
    "model": "gpt-5.4",
    "transport": "copilot_api",
    "endpoint": "responses"
  },
  "review_fallbacks": {
    "claude-opus-4.6": [
      {"model": "claude-opus-4.6-1m", "transport": "copilot_cli", "endpoint": "cli"},
      {"model": "gpt-5.3-codex", "transport": "copilot_api", "endpoint": "responses"}
    ]
  },
  "generation": {
    "max_output_tokens": 900,
    "temperature": null,
    "timeout_seconds": 600
  },
  "review": {
    "max_output_tokens": 900,
    "temperature": 0,
    "exclude_self": true,
    "timeout_seconds": 600
  },
  "synthesis": {
    "max_output_tokens": 1200,
    "temperature": 0.2,
    "timeout_seconds": 600
  },
  "review_card": {
    "max_chars": 1800,
    "max_paragraphs": 6
  },
  "retry": {
    "max_attempts": 3,
    "backoff_seconds": [2, 6]
  },
  "rubric": [
    "accuracy",
    "completeness",
    "reasoning",
    "usefulness",
    "clarity",
    "uncertainty_calibration"
  ],
  "request_timeout_seconds": 600,
  "random_seed": null,
  "models": [
    {"model": "gpt-5.4", "transport": "copilot_api", "endpoint": "responses"},
    {"model": "claude-opus-4.6", "transport": "copilot_api", "endpoint": "chat_completions"},
    {
      "model": "claude-opus-4.6-1m",
      "transport": "copilot_cli",
      "endpoint": "cli",
      "fallbacks": [
        {"model": "gpt-5.3-codex", "transport": "copilot_api", "endpoint": "responses"}
      ]
    }
  ],
  "personas": [
    {"id": "first_principles", "label": "First principles", "brief": "Re-derive from goals, constraints, incentives, and fundamentals. Challenge assumptions."},
    {"id": "contrarian", "label": "Contrarian", "brief": "Find the strongest objections, failure modes, and hidden assumptions."},
    {"id": "executor", "label": "Executor", "brief": "Turn ideas into practical sequencing, tradeoffs, and next actions."}
  ],
  "runtime": {
    "min_successful_seats": 3,
    "continue_on_partial_failure": true
  },
  "summary": {
    "max_chars": 12000,
    "top_n": 5,
    "max_list_items": 5,
    "answer_chars": 4000,
    "question_chars": 500
  }
}
```

The default operating shape is matrix-first: `models[] × personas[]` expands into explicit seats. Legacy `roster[]` input is still supported for compatibility, but it is no longer the preferred documented shape.

## Endpoint and transport routing contract

The engine must normalize requested routing into:
- transport: `copilot_api` or `copilot_cli`
- endpoint: `responses`, `chat_completions`, or `cli`

Rules:
- explicit config transport/endpoint wins
- GPT-5/Codex-like models default to `copilot_api` + `responses`
- Claude-family API variants default to `copilot_api` + `chat_completions`
- some models may be reachable only through GitHub Copilot CLI; these should use `copilot_cli` + `cli`
- catalog metadata can refine inference for API-backed models
- unknown models without clear inference should fail loudly during smoke or run setup
- roster rows may declare seat-level fallbacks; artifacts must show requested roster and resolved runtime roster separately
- review fallbacks are configured via top-level `review_fallbacks` keyed by requested reviewer model
- review substitutions must be recorded in artifacts
- retry policy and stage-specific timeouts are part of the runtime contract, not ad hoc behavior

## Matrix expansion contract

When `models[]` is present, the engine multiplies each model row by each persona row to create explicit seats.

- default matrix: 3 models × 3 personas = 9 seats
- default personas: `first_principles`, `contrarian`, `executor`
- each expanded seat gets a stable `seat_id = <model>__<persona>` unless explicitly overridden
- requested artifacts are per-seat rows after expansion, not the compact input arrays
- resolved runtime artifacts preserve per-seat requested vs resolved routing so substitutions stay auditable

Each persona row may include:
- `id`
- `label`
- `brief`

## Data contracts

### Stage 1 candidate answer object

```json
{
  "model": "gpt-5.4",
  "transport": "copilot_api",
  "endpoint": "responses",
  "role": "first_principles",
  "role_label": "First principles",
  "seat_id": "gpt-5.4__first_principles",
  "requested_model": "gpt-5.4",
  "resolution_reason": "configured",
  "ok": true,
  "answer_text": "...",
  "review_card": "...",
  "latency_ms": 1234,
  "usage": {"input_tokens": 100, "output_tokens": 400},
  "raw_response": {...},
  "attempts": [{"attempt": 1, "ok": true}],
  "error": null,
  "label": "B"
}
```

### Stage 2 review object

The reviewer prompt must request strict JSON. Normalized parsed review:

```json
{
  "reviewer_model": "claude-sonnet-4.6",
  "reviewer_transport": "copilot_api",
  "reviewer_endpoint": "chat_completions",
  "reviewer_role": "contrarian",
  "reviewer_requested_model": "claude-opus-4.6",
  "review_resolution_reason": "configured",
  "ranking": ["B", "A"],
  "best_answer": "B",
  "best_answer_why": "...",
  "scores": {
    "A": {"accuracy": 7, "completeness": 8, "reasoning": 7, "usefulness": 7, "clarity": 8, "uncertainty_calibration": 7},
    "B": {"accuracy": 9, "completeness": 9, "reasoning": 9, "usefulness": 8, "clarity": 8, "uncertainty_calibration": 8}
  },
  "critique_by_answer": {
    "A": "...",
    "B": "..."
  },
  "collective_blind_spot": "...",
  "unresolved_disagreements": ["..."],
  "normalization_warnings": [],
  "fallback_events": [],
  "attempts": [{"attempt": 1, "ok": true}],
  "raw_response": {...}
}
```

### Stage 2 aggregate object

```json
{
  "review_count": 3,
  "ranking": ["B", "A", "C"],
  "by_answer": {
    "A": {
      "borda_points": 5,
      "first_place_votes": 1,
      "review_count": 3,
      "dimension_means": {...},
      "overall_mean": 7.8,
      "critiques": ["..."]
    }
  },
  "unresolved_disagreements": ["..."],
  "collective_blind_spots": ["..."],
  "collective_blind_spot": "...",
  "normalization_warnings": []
}
```

### Final result object

The top-level artifact contains:
- `timestamp`
- `question`
- `mode`
- `github_identity`
- `catalog_model_count`
- `requested_roster`
- `resolved_roster`
- `roster`
- `chairman`
- `summary_config`
- `stage1`
- `stage2`
- `stage3`
- `failures`
- `compact_summary`
- `artifacts`

Artifact path object:
- `run_dir`
- `result_json`
- `summary_md`

## Stage protocol

### Stage 1: first opinions
Input:
- original question
- council roster
- explicit role lens per member

Behavior:
- run each member in parallel
- each member answers through its assigned role lens
- collect raw responses, parsed text, usage, latency, routing metadata, retry attempts, and failures
- generate a compact review card from each answer for downstream peer review
- assign randomized anonymous labels after responses arrive

Output:
- successful candidates plus any failures
- full answers for chairman synthesis
- compact review cards for reviewer efficiency

### Stage 2a: peer review mode
For each successful candidate answer's originating model:
- build anonymized review-card bundle instead of sending full answers when possible
- if `exclude_self=true`, omit the reviewer's own answer from the bundle
- request strict JSON ranking/scoring plus collective blind-spot extraction
- parse and validate review output
- tolerate partial/malformed review payloads when enough signal remains to normalize ranking and scores
- if the configured reviewer repeatedly times out, try configured review-fallback models/transports before recording failure

### Stage 2b: judge mode
- use only the chairman model as judge
- score all candidate review cards with the same rubric
- aggregate as if there were one review

### Stage 2c: collect mode
- skip peer review aggregation work beyond a synthetic ranking of available labels
- record zero reviews and zero review substitutions
- skip chairman synthesis and return the first sorted candidate label as the final answer payload
- record the skipped-synthesis reason in `stage3.error`

### Stage 3: chairman synthesis
Input:
- original question
- anonymized candidate answers
- aggregate ranking and score summary
- collective blind-spot summary
- requested roster and resolved runtime roster
- concise critique summary

Output:
- final plain-text/Markdown answer
- should explicitly surface uncertainty if the council disagreed
- should make substitutions visible when they materially affect confidence

## Review aggregation

Use a simple, auditable method in v1:
- Borda count from each ranking list
- mean per-dimension score by answer
- overall mean as the average of dimension means
- first-place vote count

Sort by:
1. Borda points descending
2. first-place votes descending
3. overall mean descending
4. label ascending

## Prompt contracts

### Generation prompt
- ask for a strong standalone answer
- forbid mention of the council
- prefer direct, technical, uncertainty-aware output

### Review prompt
- ask for ranking plus per-answer rubric scores
- require JSON only
- remind the reviewer to judge substance over style
- preserve unresolved disagreements

### Chairman prompt
- ask for one best final answer, not a meta-discussion
- use the aggregate ranking and critiques as signals, not absolute truth
- include explicit uncertainty/disagreement section when warranted

## Failure handling

The run must degrade gracefully, but the live runtime now enforces a configurable floor before proceeding.

If a model fails in Stage 1:
- record failure
- require at least `runtime.min_successful_seats` successful seats before continuing
- the default template sets `runtime.min_successful_seats = 3`
- if the surviving seat count is below that floor, the run aborts
- if the configured floor allows a single surviving answer to continue, review is skipped and that answer becomes the provisional final answer

If a review fails to parse or normalize:
- record raw response, attempts, and fallback events when available
- exclude that review from aggregation
- continue with any remaining valid reviews
- preserve `normalization_warnings` when salvage succeeds

If the chairman call fails:
- fall back to the top-ranked candidate answer and record the failure

Note: `runtime.continue_on_partial_failure` currently exists in the template/config surface but is not wired into runtime branching yet. Treat it as reserved/no-op unless implementation changes land.

## Artifact policy

Default artifact root:
- `assets/runs/`

Per run:
- timestamped directory name with slugged question prefix
- `result.json`
- `summary.md`

The summary is a compact operator report, not a full raw dump. It should contain:
- timestamp, GitHub identity, mode, requested matrix counts, and successful/failed seat counts
- compacted question
- requested matrix grouped by model
- runtime roster substitutions or an explicit "all seats resolved as requested" statement
- top-ranked seats only (bounded by summary config)
- collective blind spots
- unresolved disagreements
- failures
- final answer

Input config uses the key `summary`; persisted result artifacts expose the resolved values as `summary_config`.

## Testing plan

### Unit tests
- endpoint inference for GPT-5 and Claude families
- extraction of text from `/responses`
- extraction of text from `/chat/completions`
- parsing JSON embedded in code fences
- aggregation ordering and score math
- validation of malformed rankings

### Smoke tests
- `gpt-5.4` via Copilot API `/responses`
- `claude-opus-4.6` via Copilot API `/chat/completions`
- `claude-opus-4.6-1m` via GitHub Copilot CLI
- if the CLI path is unavailable, verify fallback resolution to `gpt-5.3-codex`
- verify retry metadata appears when transient failures occur
- verify review-card compaction reduces review payload size relative to full answers

### Real runs
Run at least two hard, user-relevant questions and inspect:
- whether the final answer is materially better than a single weak answer
- whether the review stage produces useful disagreement signals
- whether artifacts are complete and readable

## Acceptance criteria

The implementation is complete when:
- `catalog` works on the active token
- `smoke` passes for the default roster
- unit tests pass
- at least one `peer` council run completes end-to-end
- at least one hard, user-relevant question has artifacts and a usable final summary
