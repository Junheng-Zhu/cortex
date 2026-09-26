# Skill System V2

## Scope

V2 keeps Cortex's Responses API, DECIDE → ACT → OBSERVE → REFLECT loop, and subprocess `ToolExecutor`. It does not add LangChain, a reranking LLM/service, generated Skills, multiple agents, or a marketplace. No performance improvement is assumed: retrieval and end-to-end task results are reported separately.

## Discovery, identity, and immutable package versions

Each configured root has a source namespace (the root name by default, explicitly overridable with `source_namespaces`). A stable ID is `percent-encoded-namespace:percent-encoded-relative/path`; encoding preserves path identity instead of lossy slug replacement. Duplicate IDs are removed from the active registry with a `duplicate_id` diagnostic, never overwritten by dictionary insertion. Duplicate display names are diagnosed and require ID-based access.

Front matter is parsed with `yaml.safe_load`. `name` and `description` must be non-empty strings, multiline descriptions are supported, and `disable-model-invocation` is an optional boolean. Every bad package produces an independent diagnostic while other packages remain available. Discovery accepts regular `SKILL.md` and regular files below `references/`, rejects symlinks and special files, enforces per-file/package byte limits, and never imports package code.

The version is a SHA-256 over the stable ID and canonical manifest of every supported file path and file hash. A resource-only edit therefore changes the version. Discovery atomically publishes `.cortex/skill-snapshots/<version>/` with the manifest and exact files. Core and resources are always read from that snapshot. A new process can resolve a checkpoint version without the original directory; missing, identity-mismatched, or hash-corrupt snapshots fail explicitly.

## Retrieval and indexing

`BM25SkillIndex` precomputes tokens, per-document term frequencies, document frequencies, lengths, and average length. A query only scores those cached structures. Calling `SkillRegistry.scan()` advances a registry generation and deterministic registry version; the index rebuilds when that version changes. Both metadata-only and metadata+body configurations remain supported.

**Indexing a body is not disclosing a body.** It changes the local feature space used for ranking. The model receives only candidate ID/name/description/version until it chooses `skill_load`; only then does exact core text enter a request.

`DenseSkillIndex` uses the replaceable `EmbeddingBackend` protocol. `OpenAIEmbeddingBackend(model=...)` is the configurable real backend. Its persistent cache key includes the complete package hash, backend/model identity, and index-body configuration. Backend/build/query failures set an explicit `degraded` status and reason. `HybridSkillIndex` uses reciprocal-rank fusion (RRF); when dense retrieval is unavailable it transparently returns sparse results while retaining the degradation record. Default BM25 has no network dependency.

The initial user query is recalled locally before the first model request. The main model—not a separate reranker—may load a candidate, decline all candidates, or spend the single supplemental-search budget on a model-authored query. Trace data separates `local_query_number` from `extra_llm_requests`; a search tool call still consumes the normal global action budget.

## Disclosure and context lifecycle

State distinguishes:

1. `skill_versions`: selected exact versions;
2. `skill_bodies`: bodies loaded from verified snapshots;
3. `skill_pending_disclosures`: versioned bodies awaiting a provider request;
4. `skill_accepted_disclosures`: bodies included in a successful request;
5. `skill_resident`: bodies in the current client-managed working set.

Workers only return structured data. The main process owns all transitions. `ContextManager.build_context` prepares a request but does **not** commit pending disclosure. `AgentLoop._request` commits it only after provider success; exceptions leave it pending and trace `llm_error`. In server-managed mode only pending versions are sent, so loading a second Skill does not resend the first. A response without a reliable response ID does not prove server acceptance. In client-managed mode the working set is rebuilt with one copy of every active body; compaction can remove transcript material but not those bodies. Active body count and aggregate character budgets fail explicitly rather than truncating.

Skill content is sent as package-provided user-level guidance alongside a host-owned policy stating that it is subordinate to system/developer policy and the user's task. It never becomes host system policy and cannot grant tools or permissions.

## Resources, permissions, and `disable-model-invocation`

Search, load, and resource read retain separate host permissions. Explicit load and checkpoint restore recheck `SKILL_LOAD`; automatic search rechecks `SKILL_SEARCH`; resources recheck `SKILL_READ_RESOURCE`. Before execution, an omitted resource version is bound by the main process to the selected run version. A conflicting requested version fails; tools never fall back to the registry's newest package. Snapshot manifest membership, regular-file status, byte hash, UTF-8 decoding, and character budget are checked again on read.

`disable-model-invocation: true` removes a Skill from model-visible automatic candidates but still allows a host-authorized explicit preload. Loading Skill text does not enable any tools named or requested by that text. These controls are document boundaries, not a general sandbox; in particular the Shell blacklist must not be described as a complete sandbox.

## Checkpoint and provider cursor consistency

V2 checkpoints add schema version, pending input, controlled client history, selected versions, pending/accepted disclosures, local-search count, and remaining supplemental-search budget. This preserves function-call outputs and their `call_id`. V1 checkpoint JSON remains structurally readable because all V2 fields have defaults. A V1 content hash is not silently reinterpreted as a V2 full-package hash: unless an administrator has retained/imported a matching addressed snapshot, restore fails explicitly. Its absent disclosure proof is always treated as pending.

On restore, every package is resolved and verified and current `SKILL_LOAD` permission is checked. An accepted server disclosure is trusted only when the session still owns a provider cursor. Without that cursor the version is moved back to pending and reconstructed from the snapshot; no claim is made that deleting local history deletes provider history.

## Observability

`skill_retrieval` includes run ID through the recorder, candidate IDs/scores, index version, local query count, index build/query timing, embedding cache hits/misses, degradation and failure reason. `skill_selection`, `skill_disclosure`, `skill_resource`, `action`, and `llm_error` connect package versions, call IDs, loaded/disclosed character counts, duration, validation, and failures. `skill_outcome` records total local searches and extra model requests.

## Reproducible evaluation

Offline retrieval input is a versioned JSON `corpus` / `queries` / `qrels` file. `--adapter skillret` also accepts `skills` plus per-query `relevant_skill_ids`. The committed fixture is intentionally a smoke dataset, not a claimed benchmark:

```bash
python eval/skill_retrieval.py \
  --dataset eval/skill_retrieval_fixture.json \
  --output eval/skill_retrieval_results.json

# Real dense and hybrid retrieval (requires configured OpenAI credentials)
python eval/skill_retrieval.py \
  --dataset /path/to/fixed-skillret.json --adapter skillret \
  --dense-backend openai --embedding-model text-embedding-3-small \
  --output eval/skillret-results.json
```

Reports contain Recall@5, MRR@10, multi-Skill complete coverage@5, hot-query p50/p95, cold index time, dataset revision, cache/degradation details, and all four configurations. The fixed test partition must not be used to tune retrieval parameters.

The real-model runner follows `eval/run_baseline.py` and writes a result file only after execution:

```bash
python eval/run_skill_e2e.py \
  --tasks eval/skill_e2e_tasks.json \
  --model "$MODEL_NAME" \
  --input-cost-per-million 0 --output-cost-per-million 0 \
  --output eval/skill_e2e_results.json
```

It runs isolated temporary sessions with Memory retrieval disabled and identical model, permissions, task catalog, and runtime budget for three cohorts: no Skill, correct preloaded Skill, and automatic retrieval. It reports success rate, regression task IDs, input/output/total tokens, total latency, and configured cost per successful task. The repository does not include invented real-model results; the command must actually run to create them.

Retrieval accuracy cannot prove that Skills improve task completion. Recall only shows that relevant metadata appeared near the top: the model may decline it, load the wrong candidate, misapply correct instructions, spend too many tokens, or regress on tasks that did not need a Skill. Conversely a task may succeed from model knowledge despite a miss. Therefore retrieval metrics diagnose the recall layer, while the controlled three-cohort evaluation measures net task benefit and cost.
