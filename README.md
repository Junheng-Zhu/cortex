# Cortex

> A framework-light Agent Runtime / Harness for Agent Systems Engineering.

Cortex is a Python agent runtime focused on the engineering problems behind reliable tool-using agents: execution control, tool permissions, context lifecycle, Skills, memory, recovery, observability, and evaluation.

The project intentionally avoids heavy agent frameworks. The core runtime is explicit and inspectable, making the control flow suitable for systems learning, debugging, experimentation, and architecture discussion.

## Highlights

- **Agent Runtime State Machine** — `DECIDE -> ACT -> OBSERVE -> REFLECT -> FINAL`
- **OpenAI Responses API** — function calling with provider response continuity
- **Typed Tool Runtime** — registry, validation, permission checks, timeout/retry, structured results
- **Local + Docker Execution Backends** — one tool interface with replaceable execution backends
- **Sandboxed Docker Runtime** — network isolation, read-only rootfs, dropped capabilities, resource limits
- **Async Tool Execution** — bounded concurrency, parallel-safe waves, serial barriers, cancellation cleanup
- **Skill System V2** — BM25 / Dense / Hybrid RRF retrieval with progressive disclosure
- **Context + Memory** — session state, durable memory, checkpoints, compaction, artifact references
- **Observability + Evals** — structured traces, deterministic smoke tests, online agent evaluation

## Architecture

```text
User Request
    |
    v
Context / Memory / Skill Preparation
    |
    v
DECIDE ---> LLM / Responses API
    |
    v
ACT ------> ToolExecutor ------> Tool Registry
    |                              |
    |                              +-- Note / Artifact Tools
    |                              +-- Skill Tools
    |                              +-- Shell Tool
    |                                    |
    |                            +-------+-------+
    |                            v               v
    |                       LocalBackend    DockerBackend
    v
OBSERVE
    |
    v
REFLECT ---> CONTINUE / REPLAN / ABORT
    |
    +-----------------> FINAL
```

## Runtime Design

### 1. Explicit Agent Loop

`cortex/runtime/loop.py` implements the runtime as an explicit state machine rather than hiding control flow inside a framework.

A run owns:

- current phase and step budget;
- pending provider input and response cursor;
- completed actions and observations;
- selected Skill versions and disclosure state;
- session history, compact summaries, artifacts, and checkpoints.

The synchronous and asynchronous paths share the same semantic phases, while the async path can execute compatible tool calls concurrently.

### 2. Tool System

Tools are registered as objects instead of bare functions. The runtime separates:

```text
Tool definition
    |
    v
ToolRegistry
    |
    v
ToolExecutor
    |
    v
Permission / Validation / Timeout / Retry
    |
    v
Structured ToolResult
```

Permissions currently include:

`READ`, `WRITE`, `DELETE`, `EXECUTE`, `SKILL_SEARCH`, `SKILL_LOAD`, and `SKILL_READ_RESOURCE`.

This keeps tool capability discovery separate from execution policy.

### 3. Execution Runtime

The shell tool targets an `ExecutionBackend` abstraction.

Two backends are available:

| Backend | Purpose |
| --- | --- |
| `LocalBackend` | Fast local development and debugging |
| `DockerBackend` | Isolated command execution inside a disposable container |

The Docker backend is session-scoped and lazily creates its container on first use. Commands reuse the same sandbox until the backend is closed or a fatal execution error invalidates the container.

The sandbox applies:

- writable project workspace mounted at `/workspace`;
- read-only container root filesystem;
- disabled network by default;
- non-root execution;
- all Linux capabilities dropped;
- `no-new-privileges`;
- memory / CPU / PID limits;
- bounded command output;
- timeout-driven container disposal.

The Docker daemon and host kernel are still part of the trusted computing base.

### 4. Async Runtime

Multi-tool model responses are converted into ordered execution waves.

```text
parallel-safe wave
      |
      v
commit all observations in original call order
      |
      v
REFLECT
      |
      v
CONTINUE / REPLAN / ABORT
      |
      v
next wave
```

The executor owns a shared semaphore for bounded concurrency and a lock for serial tools. This prevents multiple concurrent runs from bypassing global execution limits or racing on shared backends.

Cancellation also propagates into process/container cleanup so abandoned work does not retain runtime ownership.

### 5. Skill System V2

Skills are discovered from `SKILL.md` packages and treated as versioned runtime resources rather than prompt text that is always injected.

The retrieval pipeline supports:

- **BM25** sparse retrieval;
- **Dense Retrieval** through a replaceable embedding backend;
- **Hybrid Retrieval** using Reciprocal Rank Fusion (RRF);
- metadata-only or metadata + body indexing;
- persistent embedding cache;
- explicit degradation when dense retrieval is unavailable.

Retrieval and disclosure are intentionally separated:

```text
User Query
   |
   v
local retrieval
   |
   v
candidate metadata
   |
   v
model chooses skill_load
   |
   v
verified versioned Skill body
   |
   v
provider context
```

A Skill body is disclosed only after selection. Cortex tracks pending, accepted, and resident disclosures so the same Skill does not have to be blindly resent on every provider request.

Skill packages are content-addressed with SHA-256 snapshots. Checkpoint restoration resolves the exact historical version instead of silently loading the newest local copy.

### 6. Context, Sessions, and Memory

Cortex separates runtime state from model-visible context.

The repository contains dedicated components for:

- context budgets and compaction;
- artifact storage and partial artifact reads;
- SQLite-backed durable memory;
- episodic session storage;
- checkpoint persistence and resume;
- memory scopes, retrieval gates, and consolidation.

This makes context construction an explicit runtime responsibility rather than an ever-growing conversation list.

### 7. Observability and Evaluation

`RunRecorder` emits structured runtime events for LLM requests, actions, observations, Skill retrieval/disclosure, failures, latency, and termination.

The evaluation layer includes:

- deterministic smoke evaluation;
- trace-based behavior evaluation;
- fixed-task online model evaluation;
- Skill retrieval evaluation;
- Skill end-to-end three-cohort evaluation.

Core agent metrics include task success rate, tool-selection accuracy, argument-valid rate, average steps, p50/p95 latency, and tokens per successful task.

Skill retrieval evaluation reports metrics such as Recall@5, MRR@10, multi-Skill completeness, NDCG/MAP, and retrieval latency.

## Repository Layout

```text
cortex/
|-- cortex/
|   |-- app/             # bootstrap and CLI
|   |-- context/         # context budgets, compaction, artifacts
|   |-- execution/       # Local/Docker execution backends
|   |-- llm/             # Responses API client and protocol parsing
|   |-- memory/          # durable memory and session storage
|   |-- observability/   # tracing and dashboard
|   |-- orchestration/   # routing, tasks, handoff primitives
|   |-- runtime/         # loop, state, actions, observations, checkpoints
|   |-- skills/          # Skill discovery, retrieval, loading
|   `-- tools/           # tool abstraction, registry, executor, permissions
|-- docker/
|   `-- execution-runtime/
|-- docs/
|-- eval/
|-- skills/
|-- tests/
|-- main.py
`-- requirements.txt
```

## Quick Start

### 1. Clone

```bash
git clone https://github.com/Junheng-Zhu/cortex.git
cd cortex
```

### 2. Create an environment and install dependencies

Windows PowerShell:

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 3. Configure the model provider

Create a local `.env` file:

```env
OPENAI_API_KEY=your_api_key
MODEL_NAME=gpt-4o-mini
# OPENAI_BASE_URL=https://api.openai.com/v1
```

`OPENAI_BASE_URL` and `MODEL_NAME` can be replaced for compatible providers.

### 4. Run Cortex

```powershell
python main.py
```

The default application starts an interactive tool-using agent.

## Docker Execution Backend

Build the execution image:

```powershell
docker build -t cortex-execution-runtime:v1 .\docker\execution-runtime
```

At construction time, `build_agent(..., execution_backend="docker")` selects the Docker backend. The default local mode uses `LocalBackend`.

A Docker installation and accessible Docker daemon are required for the sandbox backend.

## Tests

Run the full test suite:

```powershell
pytest -q
```

Representative coverage includes:

- tool runtime and permission handling;
- Responses API protocol parsing;
- multi-step agent execution;
- async runtime correctness and recovery;
- local/Docker execution behavior;
- Skill System V1/V2;
- context engineering;
- memory/session/checkpoint behavior;
- evaluation metrics.

## Evaluation

Deterministic smoke evaluation:

```powershell
python .\eval\smoke_eval.py
```

Online baseline with a real model:

```powershell
python .\eval\run_baseline.py
```

Skill retrieval smoke benchmark:

```powershell
python .\eval\skill_retrieval.py --dataset .\eval\skill_retrieval_fixture.json --output .\eval\skill_retrieval_results.json
```

SkillRet BM25 benchmark:

```powershell
python .\eval\skill_retrieval.py --dataset skillret --adapter skillret --revision a050ad2 --dense-backend none --output .\eval\skillret_bm25_results.json
```

See `eval/README.md` and `docs/skill-system-v2.md` for evaluation methodology and reproducibility requirements.

## Design Principles

1. **Runtime behavior should be explicit.** Agent control flow should be inspectable and testable.
2. **Tools are capabilities, not arbitrary functions.** Validation, permissions, retry policy, and execution ownership belong to the runtime.
3. **Execution needs a real boundary.** A blacklist is not a sandbox; isolated execution belongs in the backend.
4. **Context is a managed resource.** Memory, Skills, artifacts, and provider history should not be blindly concatenated.
5. **Concurrency needs ownership.** Timeouts, cancellation, containers, and subprocesses must have clear lifecycle owners.
6. **Retrieval quality is not task quality.** Skill recall metrics and end-to-end agent success are evaluated separately.
7. **Agent quality should be measurable.** Trace behavior, latency, token usage, tool correctness, and regression cases are first-class outputs.

## Engineering Notes

Detailed design documents are available under `docs/`:

- `execution-runtime-v1.md`
- `execution-runtime-v2.md`
- `async-tool-runtime-v1.md`
- `async-runtime-v1.1.md`
- `skill-system-v1.md`
- `skill-system-v2.md`

---

Cortex is an engineering-focused learning and experimentation project for building understandable, testable, and increasingly production-like agent runtimes.

Distributed execution is opt-in. See [Distributed Worker Runtime V1](docs/distributed-worker-runtime-v1.md) for Redis setup, independent workers, recovery semantics and local multi-process tests.
