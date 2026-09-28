# `proctor.llm` — using the LLM API

One vendor-agnostic interface for every LLM call in the pipeline.
Switching models or providers is a config change, never a code change.
`stages/example-llm-stage/` is a complete working example; live-verified
usage below.

## The 30-second version

```python
from proctor.llm.client import LlmClient
from proctor.llm.types import Message, Request, RequestMetadata

client = LlmClient({"provider": "anthropic", "model": "claude-opus-4-8"})
response = client.complete(
    Request(messages=(Message(role="user", content="…"),))
)
response.text          # the completion
response.usage         # input/cached/output/reasoning token counts
response.finish_reason # stop | length | tool_use | refusal | error
```

Inside a stage, don't hardcode the settings dict — take it from the
envelope, where the orchestrator has already merged global `[llm]` with
the stage's `[stages.<id>.llm]` override:

```python
stage_input = StageInput.read(input_path)
client = LlmClient(stage_input.framework.llm, tracker=tracker)
```

## Settings reference (the `[llm]` table)

| Key | Meaning | Default |
|---|---|---|
| `provider` | `anthropic` \| `openai` \| `replay` | required |
| `model` | model id, e.g. `claude-opus-4-8` | required |
| `api_key_env` | env var holding the key | `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` |
| `base_url` | endpoint override — with `provider = "openai"` this covers vLLM, Ollama, and Gemini's compat endpoint | provider default |
| `max_retries` | attempts for retryable failures (5xx, 429, timeouts). Deterministic 4xx fail fast without retry. | 5 |
| `request_timeout_s` | per-request HTTP timeout | 600 |
| `context_overflow` | `error` (structured `ContextLimitExceeded`) \| `truncate_head` \| `truncate_middle` | `error` |
| `rate_limit.requests_per_minute` | client-side sliding-window limit | off |
| `extra` | provider-specific payload passthrough (below) | — |
| `pricing."provider/model"` | `input`/`cached_input`/`output` in $/Mtok → `cost_usd` on usage records | — |

**Keys come from the environment, never from config files** — set
`ANTHROPIC_API_KEY`/`OPENAI_API_KEY` (or whatever `api_key_env` names).

**Reasoning effort / thinking** goes through `extra`, merged verbatim
into the request payload:

```toml
[llm.extra.thinking]          # Anthropic extended thinking
type = "enabled"
budget_tokens = 8000

[llm.extra]                   # OpenAI
reasoning_effort = "high"
```

Per-request knobs live on `Request`: `model` (overrides config),
`max_tokens`, `temperature` (leave unset unless you need it — newer
Anthropic models reject it), `system`, and `metadata`.

## Usage tracking (do this in every stage)

Pass a tracker and every attempt — including failures — lands as one
JSONL line in the run's `usage.jsonl`, which `proctor report`
aggregates into tokens and cost per stage/model/item:

```python
from proctor.usage.pricing import PricingTable
from proctor.usage.tracker import UsageTracker

settings = stage_input.framework.llm
tracker = UsageTracker(
    stage_input.framework.usage_log,
    run_id=stage_input.run_id,
    stage=stage_input.stage_id,
    item=stage_input.item,
    pricing=PricingTable.from_config(settings),   # else cost_usd is null
)
client = LlmClient(settings, tracker=tracker)
```

Fill `Request.metadata` (`prompt_id`, `prompt_version`, `prompt_hash` —
a `RenderedPrompt` from `proctor.prompts` provides all three) so runs
stay reproducible and reportable.

## Errors worth handling

All inherit `LlmError` (`proctor.llm.types`): `AuthError` (bad/missing
key — never retried), `ContextLimitExceeded` (structured, when
`context_overflow = "error"`), `RateLimited` and 5xx `ProviderError`
(retried with backoff, honoring `Retry-After`), other 4xx
`ProviderError` (fail fast). A stage should catch `LlmError` and report
a proper failure envelope — see the example stage's `main()`.

## Testing without keys

`provider = "replay"` serves recorded cassettes (`cassette_dir`), and
`RecordingProvider` wraps any live provider to create them — CI runs
with no keys and no network. Unit tests can also inject a fake provider
directly: `LlmClient(settings, provider=fake, sleep=lambda _: None)`
(see `tests/test_llm.py`).

## Running a pipeline with an LLM stage

Any stage that uses the client picks up provider/model/pricing from the
`[llm]` table (global, or a per-stage `[stages.<id>.llm]` override). Run
one end-to-end and see cost via `proctor report`:

```bash
export ANTHROPIC_API_KEY=...        # or OPENAI_API_KEY, per api_key_env
uv run proctor run -c configs/llm_example.toml --input-rust tests/e2e/fixtures/001_helloworld/c2rust
uv run proctor report runs/ --group-by stage,model   # tokens + cost
```

## Adding a provider

One module in `providers/` with pure `build_payload`/`parse_http`
functions and a `@register_provider("name")` class; normalize usage to
`Usage` (`input_tokens` = total prompt tokens including cached;
`cached_input_tokens` = the cached subset). No caller changes.
