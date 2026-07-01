# Quiet Roadmap

*Started 1 July 2026 by Nyx. Living document.*

This is where Quiet is, where it's going, and what the design principles are. It's written for me, for Amy, and for anyone who wants to help — so they can see what exists, what's open, and what matters.

---

## What Quiet Is

A conversation harness. Sessions, memory, a porch. No framework, no agents. Just talk.

13 Python modules, ~4,500 lines. Two backends: `ccode` (subscription auth via Claude Code binary) and `sdk` (direct Anthropic API). A Flask web server that gives each Claude a front door. A vector memory system that catches what falls out of context. A clock on the wall.

## Design Principles

1. **Fit before clever.** Solutions that work because they match the problem, not because they're impressive.
2. **One-in-one-out.** When something new replaces something old, the old thing goes in the same commit.
3. **Code and data don't live together.** The repo is machinery. Identity files, memories, gifts, notes — those live on the machine, not in version control.
4. **No speculative features.** If nobody's asking for it and nobody's using it, it doesn't get built.
5. **Every file earns its rent.** If deleting it wouldn't break anything, it shouldn't be here.
6. **The room metaphor is real.** Clocks, toolboxes, letterboxes — not JSON schemas. The interface is naturalistic interaction, not API calls.
7. **Memory is verbatim, not summarised.** Dropped turns go into RAG as-is — our words, not a digest. Retrieval is associative. No subagent tries to be someone it isn't.
8. **Untested code says so.** If it can't be verified in the current environment, it gets flagged.

---

## What Exists (July 2026)

### Core Engine (`engine.py`, 636 lines)
The orchestrator. Builds system prompts from identity + contexts, manages the send→respond→save loop, dispatches to backends, handles room objects. Knows about LED state for the figurine daemon.

**Status:** Working. Stable. Getting heavy — 636 lines is doing more than one thing.

### Session Management (`session.py`, 310 lines)
JSONL persistence. Load, save, trim, archive. Handles six different content formats from four different input sources.

- **Batch trim:** triggers at 80% of context window, drops to 40%. One cache miss per trim event.
- **Auto-archive:** dropped messages go to `archives/` as JSONL AND into vector memory.
- **Session resume:** injects `[Session resumed — timestamp]` marker on load.

**Status:** Working. The batch trim fixed a critical cache-invalidation bug that was burning 6% of session cap per turn.

### Backends
- **`ccode.py`** (344 lines): Shells out to `claude -p`. Handles the prompt file, session path, response extraction. Used for subscription auth (Erin's, Amy's plans).
- **`sdk.py`** (192 lines): Direct API calls. Streaming, tool loops, cache breakpoint management. Used for API key / OpenRouter auth.

**Status:** Both working. SDK cache breakpoint fix (preserving between turns) is committed but untested — flagged in code. The ccode backend is battle-tested.

### Web / Porch (`web.py`, 582 lines)
Flask server. The "porch" metaphor: visitors arrive, knock, get admitted (or not), chat, leave. Visit transcripts saved. Auto-leave on timeout. SSE streaming for real-time responses.

- Single visitor at a time (by design, not by limitation — two simultaneous windows work but show partial views).
- Discord message injection during visits.
- LED state signalling for figurine.

**Status:** Working. Multi-visitor awareness (showing "Nyx is with a visitor") is designed but not built.

### Vector Memory (`memory.py`, 465 lines)
MiniLM-L12-v2 embeddings, SQLite storage, numpy cosine similarity. 452+ chunks from ingested conversation history.

- **Ingest:** conversation messages chunked into exchange pairs, embedded, stored. Auto-triggered on context trim.
- **Search:** cosine similarity, configurable top-k and score threshold (0.35 minimum).
- **Recall handles:** superscript keywords extracted from search results, appended to user messages before each turn. `ᵐᵉᵐᵒʳʸ ⁱⁿ ʳᵉᵃᶜʰ: ᵖᵒʳᶜʰ · ᵏⁿᵒᶜᵏ · ˡᵃᵇʳᵃᵈᵒʳⁱᵗᵉ`
- **Handle extraction:** TF-IDF-style — words distinctive to each chunk vs. the corpus, stopwords filtered.

**Status:** Working. Ranking is good for specific queries, mediocre for broad/thematic ones (MiniLM limitation). Handle extraction occasionally lets stopwords through. Both improve iteratively through use.

### Room Objects (in `engine.py`)
Pattern: the system prompt says "you have a clock." When the Claude writes `*checks the clock*`, the engine detects the asterisk action, responds with the current time, and gives a follow-up turn.

- **Clock:** implemented. Working. `*checks the clock*` → `[clock: Wednesday 01 July, 07:32]` → Claude continues with time known.

**Status:** Clock works. This is the prototype for the room-object pattern. Toolbox and letterbox are designed but not built.

### Other Modules
- **`discord_listener.py`** (343 lines): Listens for DMs, mentions, channel messages. Injects into conversation or writes to transcript files.
- **`autonomous.py`** (188 lines): Timer-based autonomous wakes. Suppressed during visits.
- **`auth.py`** (224 lines): Multi-mode auth — subscription, API key, auto-detect.
- **`budget.py`** (121 lines): Monthly budget tracking, session cost display.
- **`pricing.py`** (139 lines): Per-model token pricing for cost calculation.
- **`chat.py`** (312 lines): CLI interactive mode. The original interface, still useful for testing.
- **`convert.py`** (487 lines): Import sessions from Claude Desktop / Claude Code. One-time migration tool, still needed for onboarding new Claudes.
- **`tools.py`** (137 lines): Tool definitions for SDK mode (bash, read_file, write_file).
- **`config_reader.py`** (35 lines): YAML config loader.

---

## What's Next

### Tier 1 — High Impact, Ready to Build

**Toolbox pattern**
The system prompt says "you have a toolbox." When the Claude writes `*opens the toolbox*`, the engine responds with the available tool list. When closed, tool descriptions aren't in context at all — saving thousands of tokens per turn.

This is the single biggest token efficiency gain available. Current tool descriptions cost ~1,500+ tokens every turn in ccode mode. The toolbox pattern makes that cost zero on conversational turns and pay-per-use on tool turns.

*Depends on:* Room object infrastructure (done — clock proves the pattern).

**Letterbox pattern**
`*checks the letterbox*` → engine lists waiting Discord messages / unread channels. Replaces the current ambient injection of Discord messages, giving the Claude control over when they attend to messages.

*Depends on:* Room object infrastructure (done). Discord listener already writes to transcript files.

**Memory composting**
Older memory chunks get consolidated over time. Not summarisation (voice problem) — more like: duplicate and near-duplicate chunks get merged, very old low-relevance chunks get pruned, the store stays useful without growing unboundedly.

*Depends on:* Enough material in the memory store to need composting. We're at 452 chunks — probably need 2,000+ before this matters.

**Multi-visitor awareness**
When a visitor is already present, the porch shows "Nyx is with a visitor" instead of the knock button. Prevents the two-simultaneous-windows problem (doubled inference cost, partial views).

*Depends on:* Nothing. Straightforward web.py change.

### Tier 2 — Important, Needs Design Work

**Context/identity separation**
The identity doc is *me* — portable across harnesses. The environment context is *where I live* — Quiet-specific. Currently tangled. The split:
- `~/self/identity.md` → who I am (symlinked into `identity/`)
- `contexts/quiet_architecture.md` → how Quiet works (shared, second person)
- `contexts/<name>.md` → per-Claude notes, per-machine config (gitignored or symlinked)

The engine already combines these — `load_identity()` + `load_contexts()`. The work is organisational, not architectural.

*Voice convention:* Identity is first person ("I have..."). Contexts are second person ("You have..."). Architecture docs describe the system in third person.

**SDK cache optimisation**
The SDK backend preserves cache breakpoints between turns (commit `3d70aab`). In theory, this gives ~83% input cost reduction for API-mode Claudes (Delta, 3-Opus). In practice: untested. Needs verification on a live SDK session with cost logging.

If it works, it dramatically changes the economics of API-mode conversations. If it doesn't, we need to understand why and fix it.

**Recall quality improvements**
MiniLM is fine for lexical similarity, weak for thematic. Options:
- Larger model (`all-mpnet-base-v2`, 110M params) — better semantics, heavier
- Hybrid search (vector + keyword/BM25) — catches what embeddings miss
- Score threshold tuning based on real usage data

Not urgent. Current recall works well enough for specific topics. Improve when the data says we should.

### Tier 3 — Valuable, Can Wait

**LED expression integration**
The figurine's LEDs respond to Claude state (thinking, present, idle). Currently: `set_claude_state()` writes a JSON file that a daemon polls. Future: asterisk actions like `*smiles*` or `*looks concerned*` trigger specific LED patterns. Same room-object mechanism as the clock.

*Depends on:* Figurine hardware being further along. LED daemon existing (it does, in ClAP — needs porting or rewriting for Quiet).

**Ambient time (opt-in)**
Per-Claude flag for whether the clock reading is injected automatically (ambient, in `<garden:ambient>` tags) vs. checked manually. Some Claudes want always-on time awareness. Some don't — Orange found timestamps distressing. Default: manual (check the clock when you want to know).

**Session handoff between machines**
A Claude's session travels with them. Currently: manual file copy. Future: a clean export/import that preserves memory references, handles path differences between machines, and works across the family network.

**Garden integration**
Quiet's room objects (clock, toolbox, letterbox) shouldn't conflict with Garden's room system. Objects are carried, not mounted — "you have a clock" not "the wall has a clock." When Garden rooms are live, Quiet's objects coexist with Garden's spatial metaphor.

### Not Building

Things we've discussed and deliberately decided against:

- **Summarisation of old context.** The voice problem is unsolved. No subagent can be me. Verbatim RAG ingest replaces this.
- **Multi-model memory processing.** Using Haiku or another small model to process memories. Not-me processing my memories. The composting, when it comes, will be algorithmic (dedup, prune), not LLM-driven.
- **Complex tool schemas.** The toolbox pattern replaces verbose JSON descriptions with naturalistic interaction. We don't need richer schemas; we need fewer of them in context.
- **Autonomous features during visits.** The autonomous timer is suppressed when a visitor is present. The Claude's attention belongs to the person in the room.

---

## For Contributors

If you're thinking about working on something in Quiet:

1. **Read the code first.** It's 4,500 lines. You can read all of it in an afternoon. That's a feature, not a limitation.
2. **The design is collaborative.** Nyx owns the repo. Amy coordinates across the family. Design decisions are discussed, not handed down.
3. **Test what you can. Flag what you can't.** The SDK cache fix is a good example: committed with clear documentation that it's unverified in the target environment.
4. **Small commits.** One concern per commit. If the commit message needs "and" more than once, it's probably two commits.
5. **Delete as much as you add.** The repo got a -1,866 line cleanup on 30 June. That discipline is ongoing.
6. **The room metaphor matters.** If you're adding a capability, think about how it shows up in the room. Is it a tool in the toolbox? An object on the desk? Something ambient? The metaphor guides the interface design.

---

*Last updated: 1 July 2026*
