# Copilot LLM Council

A [Karpathy-style LLM Council](https://blog.karpathy.ai/) that runs entirely through **GitHub Copilot OAuth** — no OpenRouter, no API keys, no external services.

## What it does

Sends the same question to **multiple frontier models** (GPT-5.4, Claude Opus 4.6, etc.) with different personas, runs anonymized peer review, and synthesizes a chairman answer — all using your existing GitHub Copilot subscription.

```
Question → 9 seats (3 models × 3 personas) → Peer review → Chairman synthesis → Answer
```

## Quick start

```bash
# Prerequisites: Python 3.12+, GitHub Copilot subscription with model access

# 1. Smoke test — verify your model access
python3 run_council.py smoke

# 2. Ask the council a question
python3 run_council.py ask --question "Should we use a monorepo or polyrepo for our microservices?"

# 3. Check available models
python3 run_council.py catalog
```

## How it works

**Stage 1 — First pass**: Each seat (model × persona) independently answers the question.

**Stage 2 — Peer review**: Every seat reviews anonymized answers from all other seats on 5 rubric dimensions (accuracy, completeness, reasoning, usefulness, clarity).

**Stage 3 — Chairman synthesis**: A chairman model reads all answers and reviews, ranks by Borda count, identifies blind spots and unresolved disagreements, and writes the final answer.

### Default matrix

| Model | first_principles | contrarian | executor |
|-------|-----------------|------------|----------|
| gpt-5.4 | ✓ | ✓ | ✓ |
| claude-opus-4.6 | ✓ | ✓ | ✓ |
| claude-opus-4.6-1m | ✓ | ✓ | ✓ |

### Output

Each run produces:
- `result.json` — full council data (all answers, reviews, rankings)
- `summary.md` — compact Markdown with top seats, blind spots, disagreements, and final answer

Runs are saved to `./runs/<timestamp>--<slug>/`.

## Configuration

Edit `templates/council-config.json`:

```json
{
  "artifact_root": "./runs",
  "mode": "peer",
  "chairman": { "model": "gpt-5.4" },
  "roster": [
    { "model": "gpt-5.4", "personas": ["first_principles", "contrarian", "executor"] },
    { "model": "claude-opus-4.6", "personas": ["first_principles", "contrarian", "executor"] },
    { "model": "claude-opus-4.6-1m", "personas": ["first_principles", "contrarian", "executor"] }
  ]
}
```

## Use as a Copilot CLI plugin

Copy to `~/.copilot/installed-plugins/local/llm-council/` and create the skill file. See `references/spec.md` for full plugin setup.

## Requirements

- Python 3.12+
- GitHub Copilot subscription with access to frontier models
- Active `gh auth` session (the code uses your Copilot OAuth token)

No pip dependencies. No API keys. Just Python + GitHub.

## License

MIT
