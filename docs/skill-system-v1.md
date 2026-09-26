# Skill System V1

## Scope and package format

A Skill is a document package rooted at `SKILL.md`. Its small YAML front matter requires `name` and `description`; everything after it is the core body. Optional files (normally under `references/`) are read only on demand. Discovery reads text and metadata only: it never imports or executes package code. V1 deliberately has no vector database, separate reranking model, multi-agent selection, automatic Skill generation, or claimed performance uplift.

The stable ID is derived from the configured source-root name and relative package path. The source is recorded separately, while the SHA-256 of the complete `SKILL.md` is the immutable content version. Duplicate display names produce a diagnostic and become ambiguous by name; stable IDs remain usable.

## Retrieval and model round trips

Before the first model request, the runtime turns the **user's query** into tokens and performs local BM25 retrieval. No LLM generates the initial query and this local search adds no model request. Only candidate ID, name, description, source/version and score are recorded; the prompt receives a shorter metadata view. The main model then chooses by calling `skill_load`, may choose none by answering normally, or may spend the single supplemental-search budget by calling `skill_search` with its own improved query. A load therefore uses the ordinary DECIDE → ACT → OBSERVE loop: it causes the next normal model turn, not a hidden reranker request. Trace fields distinguish local search numbers from `llm_call` events.

`SkillRegistry(index_body=False)` indexes metadata only. With `index_body=True`, BM25 also indexes the body, improving matches for vocabulary absent from the description. Index inclusion is not disclosure: candidates still expose metadata only. The selected core is injected whole, never silently truncated. Resources remain undisclosed until `skill_read_resource`.

## State ownership, budgets, and compaction

ToolExecutor executes tools in child processes. Consequently tools return plain structured values; only `AgentLoop._apply_skill_result` in the main process mutates `AgentState`. `skill_versions` means a file/version was successfully resolved, `skill_bodies` means exact core text is in the client working set, and `skill_disclosed` records disclosure. They are not collapsed into `loaded=true`: after client-side compaction the registry version can be re-read and the core reinserted. Repeated loads of the same version are diagnosed and do not count bytes twice.

The global action budget (`max_steps`) covers searches and loads, and there is at most one supplemental `skill_search`. Candidate count, resource size, and context size are bounded independently. If an indivisible core cannot fit after compaction, context building raises an explicit error rather than clipping instructions.

In client-managed mode the exact selected bodies are rebuilt into every request working set, so compaction cannot silently erase them. In `previous_response_id` mode candidate/core additions are sent once and subsequently owned by provider history. Removing local content is **not** represented as deletion of server history; V1 does not attempt such a deletion.

## Checkpoint recovery

Checkpoints store each stable ID and exact content hash. Resume resolves that pair before continuing and restores the exact body. Registries retain versions observed during their lifetime. If a process starts with a registry that cannot provide a recorded version, resume fails explicitly with `SkillVersionMissingError`; it never substitutes the newest package silently.

## Permission and trust boundary

Search, core load, and resource read have distinct permissions (`SKILL_SEARCH`, `SKILL_LOAD`, and `SKILL_READ_RESOURCE`), checked by ToolExecutor in addition to ordinary tool execution permission handling. A Skill is instruction text and cannot grant itself a permission. Resource paths must be relative, may not contain `..`, and are resolved before a package-root containment check; absolute paths and escaping symlinks fail. This is a narrow document-access boundary. The existing Shell tool's command blacklist is defense in depth and **not a complete sandbox**.

## Evaluation switch and example

`build_agent(..., skills_enabled=False)` removes Skill tools and retrieval for an A/B evaluation. `explicit_skills=["skills:python-debugging"]` bypasses automatic selection and loads exact core content before the first request. `skill_index_body=True` selects the metadata+body indexing configuration. Real example packages live in `skills/`.

```python
from cortex.app.bootstrap import build_agent

agent = build_agent(client, skills_enabled=True, explicit_skills=["skills:python-debugging"])
print(agent.run("Help diagnose this traceback"))
```

## Interview follow-ups / trade-offs

* **Why BM25?** It is deterministic, local, cheap, inspectable, and sufficient for V1. It also makes “retrieval count” unambiguous. Multilingual tokenization is intentionally simple and can later be replaced behind the index interface.
* **Why let the main model select?** Selection sees the actual task and avoids another model/reranker dependency. The cost is a normal tool round trip only when it elects to load or search.
* **Why hash the whole document?** Front matter changes can alter selection semantics as much as body changes; a whole-document hash gives one auditable version.
* **What happens to modified packages?** New scans expose the new hash while the registry retains already observed versions in-process for checkpoints. Durable deployments should retain versioned package artifacts; missing versions fail closed.
* **Can a malicious Skill escape?** It cannot execute during scan and resource reads are contained, but selected prose is still prompt content. Installation/source review is the trust decision; this feature is not a general code sandbox.
