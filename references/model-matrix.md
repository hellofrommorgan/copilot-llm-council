# Copilot LLM Council: model matrix

## Verified on this machine during planning

These smoke tests were observed to work:
- `gpt-5.4-mini` via `/responses`
- `claude-sonnet-4.6` via `/chat/completions`

These models were visible in the active Copilot catalog at planning time:
- `gpt-5.4`
- `gpt-5.4-mini`
- `gpt-5.2`
- `gpt-5.1`
- `gpt-5-mini`
- `gpt-5.1-codex`
- `gpt-5.1-codex-max`
- `gpt-5.2-codex`
- `gpt-5.3-codex`
- `claude-opus-4.6`
- `claude-sonnet-4.6`
- `claude-opus-4.5`
- `claude-sonnet-4.5`
- `claude-sonnet-4`
- `claude-haiku-4.5`
- `gemini-2.5-pro`
- `grok-code-fast-1`
- plus several older Azure OpenAI variants

## Endpoint heuristics

### Default to `responses`
Use `responses` for:
- `gpt-5*`
- `*codex*`
- models whose catalog explicitly lists `/responses`

### Default to `chat_completions`
Use `chat_completions` for:
- `claude-*`
- models whose catalog explicitly lists `/chat/completions`

### Treat as experimental until proven
- `gemini-*`
- `grok-*`
- models with missing endpoint metadata in the catalog

Observed on this machine during implementation:
- `claude-opus-4.6` is available through the Copilot API
- `claude-opus-4.6-1m` is available through GitHub Copilot CLI on this machine
- `claude-opus-4.6-1m` did not appear in the raw Copilot API catalog probe, so the council must support mixed transport
- the third seat is configured as `claude-opus-4.6-1m` with fallback to `gpt-5.3-codex` if the CLI path is unavailable

## Recommended rosters

### Plumbing roster
Use to validate the engine before cross-vendor review:
- `gpt-5.4`
- `gpt-5.2`
- `gpt-5.4-mini`
Chairman:
- `gpt-5.4`

### Default roster
User-preferred roster, with fallback if the third seat is unavailable:
- `gpt-5.4`
- `claude-opus-4.6`
- `claude-opus-4.6-1m`
Chairman:
- `gpt-5.4`

Current effective fallback on this machine if the CLI path breaks:
- `gpt-5.4`
- `claude-opus-4.6`
- `gpt-5.3-codex`
Chairman:
- `gpt-5.4`

### Alternate balanced roster
Use when you want a slightly cheaper/more general mix:
- `gpt-5.4`
- `claude-sonnet-4.6`
- `gpt-5.4-mini`
Chairman:
- `gpt-5.4`

## Roster selection guidance

Prefer diversity, but not at the expense of endpoint stability.

Order of preference:
1. models proven by smoke tests on the active Copilot identity
2. models with explicit endpoint metadata
3. models from different vendors/families
4. avoid all-preview rosters

## Notes
- Catalog presence is not proof of runtime success.
- The active GitHub identity changes what is available.
- API-visible and CLI-visible model sets can differ.
- The default template resolves fallbacks before a run when a roster member declares `fallbacks`.
- Roles are orthogonal to models; keep perspective diversity separate from model diversity.
- Keep one cheap fallback member such as `gpt-5.3-codex` available for degraded runs.
