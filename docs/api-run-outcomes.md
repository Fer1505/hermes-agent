# API run outcomes

All four API families interpret native terminal flags through the same outcome
mapping. Assistant text may exist after a failure, interruption or partial run;
its presence does not establish successful completion. The original result and
provider transcript remain unchanged.

| Native terminal result | Session/run status | Responses status/event | Chat finish reason |
| --- | --- | --- | --- |
| Completed without contradictory flags | `completed` | `completed` / `response.completed` | `stop` |
| Failed or error-only result | `failed` | `failed` / `response.failed` | `error` |
| Interrupted, without failure | `cancelled` | `incomplete` / `response.incomplete` | `error` |
| Partial, or explicitly not completed | `incomplete` | `incomplete` / `response.incomplete` | `error` |
| Partial native output truncation | `incomplete` | `incomplete` / `response.incomplete` | `length` |

Explicit failure wins over other flags. Partial/interrupted flags override
`completed: true`. Legacy dictionaries without completion flags retain their
existing success behavior unless they carry an error. This reports the agent
turn's outcome; it is not proof of business success, a provider action, or
message delivery.

## Client handling

- Session replies expose `status`, `completed`, `partial`, `failed`,
  `interrupted`, `error` and `error_code`. `assistant.completed` means the text
  stream ended; inspect its fields. The terminal run event is one of
  `run.completed`, `run.failed`, `run.cancelled` or `run.incomplete`.
- Asynchronous run status and terminal events preserve retained output, usage,
  effective session identity and undelivered steering text for every outcome.
- Chat Completions retains the existing Hermes `error` finish reason for
  unsuccessful non-truncation outcomes, with explicit `hermes` metadata. It is
  a Hermes extension, not a standard OpenAI finish-reason enum. No usable text
  on an unsuccessful synchronous chat request returns HTTP 502.
- Responses uses supported terminal events. Native interruption is represented
  by `response.incomplete` plus Hermes interruption metadata. A documented
  `response.cancelled` event is not assumed. Known native truncation maps to
  `incomplete_details.reason: max_output_tokens`; other native causes remain in
  Hermes metadata instead of inventing OpenAI reason values.
- HTTP 200 or an ended SSE connection alone does not establish completion.
  The final status survives Responses GET and asynchronous run polling/restart.
  Read the run status before considering a retry.

Incomplete runs are terminal for idempotency retention, stop handling and
in-memory cleanup. Within retention, replaying their idempotency key returns
the original run without executing it again. Expiry keeps the existing
retention policy; in-flight reservations cannot expire into duplicate work.

Error metadata is redacted independently of the ordinary API text policy.
Null/empty final text must preserve the original failure cause. Responses
continuation uses the raw native transcript even for unsuccessful turns, so
display projection does not rewrite tool history or provider replay.

## Protocol references and validation limits

The [Responses reference](https://developers.openai.com/api/reference/python/resources/responses/methods/retrieve)
defines response statuses; the [streaming reference](https://platform.openai.com/docs/api-reference/responses-streaming/response/refusal?lang=python)
defines terminal events. The installed OpenAI Python SDK error/detail schemas
are also checked in the focused tests. These checks do not certify every
field of Hermes' OpenAI-compatible API against the full SDK schema.

This source repair still requires integrated deployment and actual consumer
acceptance. Synthetic model/executor tests establish transport behavior, not
real agent task completion or production delivery.
