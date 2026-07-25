# FLEET.md — machine roles and orchestration

*Created 2026-07-24 at the Virtual Operator arc kickoff (from Mark's kickoff brief §6, plus
the same-day CPU amendment). Every rule here was paid for by an incident in the prior arcs.
Roles and patterns are stable; verify specific paths/task names/guard versions against the
machines on first contact — this doc describes roles and patterns, not live state.*

## 1. The fleet at a glance

| Machine | GPU / compute | Role | Availability | Data locality |
|---|---|---|---|---|
| **Server** (`DESKTOP-5L867J8`) | GTX 1060 6GB; **slow/low-spec CPU** | Always-on anchor: orchestration + chain ownership, instrument evals/dumps, data home | 24/7 (the reliable box) | F:\ game archives + eval-game video; G:\ballresearch analysis outputs; F:\archive\ |
| **4070 laptop** (jared-laptop) | RTX 4070 8GB; **fast CPU** | Fastest trainer (~4–5× the 1060); big-dump node when clips are staged; **CPU-heavy batch analysis node** (amendment, Mark 2026-07-24) | **Opportunistic** — it's a laptop; may leave the network | Local staging dir (~12 GB/game for dumps); pulls stores/code from the share |
| **FORTNITE-OP** | RTX 3060 Ti 8GB | Guarded overflow trainer: queue-fed, yields to gaming | Interruptible (gaming, heat shutdowns at night) | Stores must be STAGED to local disk before jobs run |
| **Host laptop** | i7-1270P, Iris Xe 96EU | **The product-target machine.** DirectML eval path certified to-the-digit vs server | Mark's machine | — |

## 2. Job placement (decision table)

| Job class | Where | Why |
|---|---|---|
| Training runs (hn4-recipe, ~40 ep) | **4070** (1.5–2h) → F-OP queue (3–4h, interruptible) → 1060 (6h+, last resort) | Raw speed; F-OP absorbs overflow via the guard |
| Full-game candidate dumps | 4070 **if** the game's clips are staged locally (~12 GB); else 1060 | ~5h on 1060 vs ~1.5h on 4070 — but only after paying the one-time staging cost |
| Instrument evals (SPC/Iron/FAIR windows) | **1060, always** | Eval-game video lives on its local F: — moving it costs more than the GPU gains |
| **CPU-HEAVY batch analysis** (replay sweeps, oracle ladders, parameter fits, bootstraps at scale) | **4070 CPU** after staging inputs to its local disk (stage-then-run) | **Amendment (Mark 2026-07-24): the server CPU is slow/low-spec; the 4070's CPU is fast.** Inputs stay canonical on the server; results push back on completion |
| Light CPU reads (paired reads, compose_verdict, small autopsies, power sims) | Server CPU or host laptop | Small enough that locality beats speed; never occupies a trainer |
| **Product-floor numbers** (fps, DirectML recall) | **Host laptop ONLY** | It is the certified product path; a 1060/4070 number is not a product number |
| Long chains / overnight orchestration | **Server owns it**; 4070 and F-OP are workers | Only the always-on box may own a chain; laptops disappear |
| Secondary trains (seed replicates, safety pairs) | F-OP queue | The guard makes interruption free; primary results shouldn't wait on gaming |

## 3. Orchestration patterns (use these, don't reinvent)

- **Queue + guard (F-OP):** name-ordered job files; the guard dispatches when the GPU
  is free and **yields unconditionally when nvidia-smi shows a game process** —
  physical signal, never a presence beacon alone (a dead beacon once read "idle" and
  a training crashed the box mid-Fortnite). Stale beacon fails safe to "present."
  Jobs resume from best.pt across pauses.
- **Checkpoint waiter (per-arm overlap):** don't serialize train-all-then-eval-all.
  The waiter installs each checkpoint as it lands and immediately starts that arm's
  evals on the 1060 while the next arm trains on the 4070. This alone cut a verdict
  timeline by ~4 h.
- **Stage, then run:** F-OP and the 4070 pull stores/clips to LOCAL disk before the
  job starts (store-pulls inside a queue's cmd context have failed; network mounts
  mid-train are fragile). One verified copy, then compute against local data.
- **Scheduled tasks, not Start-Process:** WinRM session teardown kills child
  processes. Anything that must survive a disconnect runs as a scheduled task.
- **Monitors + ntfy:** every chain writes a status file; a monitor watches phase
  transitions and silent death; milestones ntfy Mark. Chains are checkpointed so a
  cycle/relaunch loses no completed work (skip-if-exists on builds/chunks).
- **Single owner per resource:** one writer per dump dir, one chain per GPU. The one
  parallel-dump experiment raced the chain's own dump and killed both. Parallelism
  comes from the waiter pattern across machines, not two writers on one target.

## 4. Reliability rules (each one is a scar)

1. **Verify the checkout inside the chain.** A single-branch clone made a new branch
   unfetchable; the chain silently ran old code and died an hour later on an unknown
   flag. Chains now hard-gate on HEAD + a feature probe before running anything.
2. **In automated chains, warnings don't exist** (CLAUDE.md rule 8). Guards hard-fail
   or they aren't guards — the SPC-FULL instrument shipped broken past a soft warning
   printed into a log nothing reads.
3. **Clean restart beats warm resume when the run is a comparison.** An auto-resume
   after a reboot gave one factorial cell ~70 effective epochs vs the others' 40 —
   killed and re-run from scratch. Resume is for convenience jobs, not for arms of an
   experiment.
4. **NTP on every box.** The fleet ran ~2.2 h slow for a day; artifacts carried mixed
   clocks. Ordering provenance is by git history, but don't make it necessary. Never
   sync a clock mid-pipeline — bank artifacts first.
5. **Check, don't assume — including the time and the queue state.** Minutes-old
   statuses were once misread as stale via a wrong wall-clock model; "two trainer
   processes" was once a launcher parent/child, not a double dispatch. Read the
   physical signals (nvidia-smi, PIDs, mtimes) before acting.
6. **Import-light workers.** GPU workers import leaf modules only (the package root
   is lazy via PEP 562); no worker needs the full app dependency tree.
7. **Archive when an arc closes** (e.g. F:\archive\geodet_phase2): stores, pkls,
   checkpoints, labels, logs — then the working dirs are disposable.

## 5. Etiquette for shared machines

- **F-OP is a gaming PC first.** The guard enforces it; never bypass. Expect evening
  pauses and heat-driven overnight shutdowns — the queue absorbs both. If a result is
  deadline-sensitive, it doesn't belong on F-OP.
- **The 4070 laptop is opportunistic.** Grab it when offered, stage what it needs,
  and design chains so its disappearance strands nothing (server-side waiter picks up
  whatever it pushed; unfinished work re-queues elsewhere).
- **The host laptop is the product oracle, not a worker.** Its Iris Xe numbers are
  the only real fps/floor measurements. Keep it free for that and for Mark's own
  labeling/review sessions.

## 6. Quick reference: a typical multi-arm experiment

1. Server builds the store(s) (checkpointed, skip-if-exists), publishes to the share.
2. 4070 pulls and trains arms sequentially; F-OP queue takes replicates/safety pairs.
3. Checkpoint waiter on the server installs each best.pt as it lands → that arm's
   instrument evals run on the 1060 immediately.
4. Big per-game dumps go wherever the clips are staged (4070 preferred).
5. CPU-heavy analysis on the 4070 (staged); light reads + compose_verdict on the
   server; product-floor checks on the host laptop; everything ntfys; the chain's
   status file is the single source of truth.
6. Arc closes → archive → working dirs disposable → CURRENT STATE updated.
