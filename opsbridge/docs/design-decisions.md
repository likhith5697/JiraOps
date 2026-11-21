# Design Decisions: AI-assisted self-service (`ask`)

These records explain *why* the knowledge base and `ask` feature are built the way they are. For *how* the
rest of OpsBridge works, see [architecture.md](architecture.md).

---

## DD-1: Hybrid search (keyword + semantic), not one or the other

**Context.** Support questions mix two kinds of signal:

* **Exact tokens** that only keyword search handles reliably: API names (`CustomerOrderAPI`), HTTP status
  codes (`502`), error codes, hostnames and environment names. Embeddings often blur these. `HTTP 500`
  and `HTTP 502` are almost the same vector, but they are very different problems.
* **Paraphrases** that only semantic search handles: "payments hang" vs "authorization requests time out".

**Decision.** Score every candidate with both and blend them:
`0.6 × semantic + 0.4 × keyword`, plus small domain boosts: same API +0.15, same environment +0.05,
same HTTP code +0.05, recency up to +0.05 over 90 days, and helpful feedback up to +0.05. Not-helpful
feedback costs up to −0.15. Positive boosts are capped at 1.0 *before* penalties are applied, so negative
feedback still demotes an otherwise perfect match. Boosts never qualify an entry on their own: an entry
needs real textual or semantic relevance first.

**Keyword score is absolute.** BM25 ranks are relative, so the best hit is always "1.0". On its own that
makes a minimum-score threshold meaningless. The keyword score is therefore 70% *term coverage* (the share of
the question's content words the entry contains, after stemming) and 30% BM25 rank. `ASK_MIN_SCORE`
(default 0.35) then means something: below it, OpsBridge says "I couldn't find a similar resolved issue"
instead of showing the least-bad match.

**Degrades by design.** With no embedding provider, or when embedding fails or the budget is used up, the
same pipeline runs keyword-only. Entries saved before their embedding succeeded are still found by keyword.

---

## DD-2: Grounding + citation validation (don't just *ask* the model to cite)

**Context.** A wrong answer from a support assistant is worse than no answer. It sends engineers down the wrong
path with false confidence. A system prompt that says "answer only from the sources" is necessary, but it
isn't a guarantee.

**Decision.** Three layers:

1. **Retrieval-only context.** The model sees only the retrieved entries (`ASK_ANSWER_V1`). It isn't asked
   to know anything about your systems.
2. **Prompt rules.** Answer only from the sources, cite Jira keys inline as `[OPS-12]`, name the environment of
   each fix, say so if the sources don't answer, never invent causes, at most ~120 words.
3. **Mechanical validation.** OpsBridge checks the output. It needs at least one bracketed citation, every
   bracketed key must be a retrieved ticket, and any other key of the same Jira project mentioned anywhere must
   also be retrieved. Other ID shapes, such as `CORR-123456`, are ignored. If a check fails, the answer is
   **discarded** and the deterministic keyword format is posted instead.

Every answer stores `mode` (`llm` / `keyword` / `none`), `prompt_version`, the retrieved keys and the top score,
so any answer can be traced back to its sources and its prompt. Prompts are versioned constants and are never
edited in place.

**Privacy.** Questions, problem text and resolutions are redacted *before* they're stored or sent to a provider.
That covers emails, bearer tokens and JWTs, `password=` / `api_key:` style secrets, known key prefixes, long hex
or base64 strings, and phone numbers. The gateway redacts again as a safety net. Logs record provider, model,
token counts and latency, never prompt text.

---

## DD-3: SQLite FTS5 + in-Python cosine for the POC

**Context.** The POC runs as one container on SQLite, and knowledge grows one entry per resolved ticket:
hundreds to low thousands of rows, not millions.

**Decision.**

* **Keyword:** an FTS5 virtual table (`knowledge_fts`) with the `porter unicode61` tokenizer, so *failing*,
  *fails* and *failed* match. It's an external-content index over `knowledge_entries`, kept in sync by
  `AFTER INSERT / UPDATE OF … / DELETE` triggers, so every write path stays consistent, including the
  admin delete. Ranked with `bm25()`.
* **Semantic:** vectors stored as JSON text. Cosine similarity is computed in Python over all entries with the
  current embedding model. At 2,000 entries × 768 dimensions that's about 1.5M multiply-adds per question:
  milliseconds.
* **Portability:** all of this sits behind `KnowledgeRepository`. If FTS5 isn't compiled into the SQLite
  build, or the database isn't SQLite, a portable Python keyword scorer takes over automatically.

**Why not a vector database now?** It would add a second stateful service to run, back up and secure, before
there's data that needs it. The repository boundary means adding one later is a local change.

---

## DD-4: Migration path to PostgreSQL + pgvector

Only `KnowledgeRepository` changes. The services (`KnowledgeService`, `KnowledgeSearch`, `AskService`) don't.

| Concern | POC (SQLite) | PostgreSQL |
|---|---|---|
| Keyword index | FTS5 virtual table + triggers | `tsvector` generated column (`to_tsvector('english', api_name ‖ problem ‖ resolution)`) + GIN index |
| Keyword rank | `bm25()` | `ts_rank_cd()` (or the `pg_search` / ParadeDB BM25 extension) |
| Vectors | JSON text, cosine in Python | `vector(n)` column (pgvector) + HNSW index, `ORDER BY embedding <=> :q LIMIT k` |
| Candidate set | all embedded entries | top-k from the HNSW index, then the same blending and boosts in Python |
| Feedback counters | atomic `UPDATE … SET n = n + 1` | unchanged |

Steps:
1. Switch `DATABASE_URL` and add Alembic. The additive startup migration covers new columns, but
   type changes (JSON text → `vector`) need a real migration.
2. Implement `keyword_candidates()` with `tsvector` and add a `semantic_candidates()` method that uses pgvector.
3. Re-embed with `scripts/backfill_knowledge.py`. Entries record `embedding_model`, so switching models is
   detected automatically and stale vectors are re-embedded.
4. Keep the scoring weights and threshold. They're defined on normalised 0–1 scores, so they don't depend on
   the backend.
