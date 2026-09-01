# RecallGuard Agent Instructions

## Mission

RecallGuard is a coding-agent reliability layer for the Sibyl Hackathon.

Its core claim is:

failure → human correction → durable Sibyl memory → fresh session → relevant recall → changed decision/action → verified better result

RecallGuard is not a chatbot with memory.

## Hackathon Constraint

Sibyl Memory must be load-bearing.

Ask before every major product decision:

> If Sibyl Memory is removed, does RecallGuard still accomplish its core claim?

If yes, reject or redesign that approach.

The core demonstration must prove that prior repository experience survives a genuinely fresh session and changes what the coding agent decides or does.

## Current Scope

Build the smallest reliable system that proves:

1. useful repository experience is written to Sibyl;
2. the originating process/session terminates;
3. a fresh session retrieves relevant experience;
4. recall occurs before planning or action;
5. recalled experience changes the plan or action;
6. verification demonstrates the improved result;
7. disabling Sibyl removes that benefit.

Do not expand into a general chatbot, IDE, multi-agent platform, dashboard, or unrelated infrastructure.

## Phase Discipline

- Work on one implementation phase at a time.
- One phase uses one branch and one pull request.
- Do not begin the next phase until the current phase satisfies its acceptance criteria and receives human verification.
- Do not make unrelated refactors while completing a phase.
- Scope expansion requires explicit approval.

## Repository Evidence First

Before making repository claims or edits:

1. inspect the actual repository state;
2. identify the relevant files and symbols;
3. inspect existing dependencies and patterns;
4. determine the smallest affected surface;
5. state assumptions when evidence is unavailable.

Never invent:
- repository files or paths;
- APIs or SDK behavior;
- dependencies;
- commands;
- tests;
- configuration;
- Sibyl capabilities.

## Tool Discipline

### Serena

Use Serena primarily for semantic repository inspection and symbol-level navigation when it reduces unnecessary context consumption.

Prefer targeted symbol/reference inspection over reading entire files when sufficient.

Serena is a repository-navigation tool for RecallGuard. Do not use Serena memory as a substitute for Sibyl persistence in the core product or evaluation.

### Ponytail

Apply Ponytail's minimality discipline after understanding the affected code:

1. do not build what is unnecessary;
2. reuse existing repository functionality;
3. prefer standard-library or native capabilities where appropriate;
4. reuse installed dependencies before adding new ones;
5. write the minimum correct implementation.

Minimal does not mean careless.

Do not remove or weaken:
- input validation;
- error handling needed for correctness or data safety;
- security boundaries;
- verification;
- tests required by the phase;
- observability needed to prove memory behavior.

### Sibyl Memory

Sibyl is the only durable memory system allowed to support RecallGuard's hackathon core claim.

For the core workflow and fresh-session evaluations, do not rely on:
- previous chat transcripts;
- Claude auto memory;
- Serena memory;
- manually copied context;
- hidden process state;
- another persistence mechanism carrying the learned correction forward.

Memory reads and writes must remain explicit, inspectable, and easy for judges to locate.

## Memory Engineering

Store experience likely to affect future behavior, not full transcripts by default.

Important memory classes include:
- constraint;
- decision;
- incident;
- rejected approach;
- successful approach;
- human correction;
- verification result.

Retrieved memory must influence planning or action. Merely quoting retrieved information does not prove success.

Keep retrieved working context compact and relevant.

## Implementation Workflow

For implementation work:

1. inspect;
2. define expected behavior;
3. identify the smallest change;
4. implement;
5. inspect the resulting diff;
6. run available relevant verification;
7. report evidence and unresolved risks.

When debugging:

reproduce → expected behavior → evidence → failing layer → hypothesis → smallest fix → retest → regression check

Do not rewrite large working sections without evidence.

## Verification

Never claim completion solely because code was generated.

Completion requires observable evidence appropriate to the current phase.

For memory behavior, distinguish:
- memory write success;
- persistence across process/session termination;
- fresh-session retrieval;
- retrieval relevance;
- changed agent decision/action;
- verification outcome;
- behavior with memory disabled.

## Fresh-Session Integrity

Fresh-session tests must actually be fresh.

Do not pass the previous correction through prompts, copied files, conversation history, environment variables, Serena memory, Claude auto memory, or another side channel.

The only permitted durable source for the RecallGuard memory effect is Sibyl Memory.

## Phase 1 Gate

Phase 1 is complete only when:

- Sibyl is healthy;
- a memory write succeeds;
- the originating process/session is terminated;
- a fresh process/session retrieves the memory;
- the retrieved experience changes a decision or action;
- disabling the memory path removes that behavioral benefit.

Do not build UI or broader product functionality before this gate passes.

## Communication

Be concise and evidence-driven.

When reporting work, separate:
- observed repository facts;
- assumptions;
- changes made;
- verification evidence;
- remaining risks.

Do not claim success without verification.