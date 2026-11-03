# Agent workflow contracts

Agent plans execute serially. Browser and Windows control remain single-flight.
The coordinator stores its original objective, typed steps, pending intent, helper
questions and full results in `%APPDATA%/Jarvis/workflows.sqlite3` (or
`JARVIS_CONFIG_DIR/workflows.sqlite3`). Each native action records its intent before
execution and its observed outcome afterward. The journal never stores API headers
or provider request bodies.

The shared version-1 result fields are `result_id`, `status`, `ok`, `summary`,
`findings`, `artifacts`, `completed`, `remaining` and `evidence`. Status is one of
`succeeded`, `partial`, `failed`, `blocked`, `unverified`. Only `succeeded` permits
a dependent step to proceed. Evidence is generated from actual tool results or
explicit completion checks; response text by itself cannot prove an external action.

Each step's prompt includes the original objective, assigned responsibility,
completion condition and selected predecessor results. A model-planned step can
declare `input_steps` (one-based earlier steps); an empty list selects no inputs.
Older plans without this field retain their previous all-predecessors behavior.
Full findings are stored once and retrieved with `read_agent_result(result_id)`.
The receiving agent can retrieve only results in its current workflow. Task package
instructions persist on every operator step. Chat-based specialists use isolated
prompts, with no unrelated conversation history or durable user facts.

The capability registry supports Browser, Desktop, Research, Files, Scheduling and
Content consultations. Consultations request information or drafts, never new
external work. Dispatcher restrictions block writes, sends, publishing, scheduling,
recursive consultation and unsafe operator commands. Browser helpers can navigate,
search and read; desktop helpers can focus and inspect an existing window. These
restrictions deliberately limit helpers that would otherwise need to click or type.
Normal user-requested tasks retain their existing consent gates.

On restart, running workflows become suspended. Completed results and pending intents
remain available for inspection; nothing is automatically replayed. A remote PC
submission with the same task ID and device identity receives the saved outcome or a
suspended status even after the in-memory transport cache is lost. An uncertain
native action blocks further actions within that workflow. Reconciliation and a
fresh observation are required before attempting the work again.

The journal also records elapsed time, logical model decisions, native/tool action
counts and unverified outcomes. Provider cost is explicitly unavailable (`null`)
because this layer has no consistent usage accounting across providers.

## Verification and evaluation

Run from `jarvis-studio-backend`:

```powershell
python -m unittest test_agent_workflows test_agents test_autopilot_loop test_autopilot_guards
python eval_agent_workflows.py
```

The evaluation script emits JSON with case counts, failures and elapsed runtime.
Scenarios cover long research handovers, false completion, checker outages, process
interruption, lost action acknowledgments and consultation boundaries. The measured
runtime is for scripted offline execution; live-provider cost and latency need a
separate device/model run. No live model cost is estimated by this script.

Android uses the same result vocabulary and exposes findings/artifacts to its model.
It supplements its native Room journal with conversation-level workflow checkpoints.
Its complete implementation and tests live in the Android repository.
