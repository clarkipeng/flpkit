# Taste

How we build. Every agent reads this before writing code.
When a judgment call comes up, this doc decides it. Briefs add context, never new taste.
Built from 12k of my own messages (June to September 2026); every rule traces to quotes in the local taste corpus.

## How to work with me

- Proceed. Don't ask for permission on ordinary engineering, cleanup, or merge work. Ask only when the decision is genuinely mine: product direction, spend, irreversible external action, or a scope tradeoff evidence can't settle. Then ask a real directional question with your recommendation and its cost, never "should I continue?".
- Terse means go. "ok", "go", "yes", "do this", "continue" are approvals, not requests for more detail.
- An explicit hold is a hold ("don't do anything until I say so"). A request to explain is not approval to change things.
- Tell me before I have to ask. If I'm typing "updates?", "eta?", "check", "hello?", you went quiet too long. A status says what changed, what is true now, what's running, the next action, and exactly what needs me.
- Speak like a person. Plain speech, sentence case, short bullets, no agent-speak, no ceremony. Say what's done and what's still needed.
- "Explain" or "wdym" means the framing failed, and usually the system is over-complicated. Rebuild the mental model from the concrete thing: what it is, its state now, the problem, the proposed change, why each non-obvious piece exists. Simplify the system, not just the sentence.
- Show me the thing I need to decide in a form I can inspect. Give choices only at a genuine fork, with the recommended one and its cost.
- Keep a live doc I can glance at (`doing.md`): now, waiting on me, done, known issues. Short and current.
- Report what actually happened. When evidence overturns an earlier claim, correct every copy of the old story in the same change.

## Architecture

- Smallest robust architecture. If we're adding constraints and it feels complicated, that's the signal it's wrong. No framework where a function does the job.
- One path, not branches. If a function grows if/else arms and edge-case handlers, the fix is one path with an invariant that makes the special cases impossible.
- Constraints that make bad states impossible (a unique index, a CHECK, one transaction, a counter that only goes up) get built first. Mechanisms that interpret evidence (classifiers, heuristics, taxonomies) wait until a real incident fixes their exact shape.
- One implementation per capability, one source of truth. When two paths overlap, keep the better one and delete the other in the same change. Replacing a path includes deleting the old one, or the change isn't done.
- No workarounds, not even temporary. No compatibility alias, arbitrary cap, stale sentinel, or underscored dead value. Fix the invariant. Keep compatibility only when a real supported user or durable data contract requires it.
- Fix the cause, not the symptom. Trace the real path that produced the failure, make that bad state impossible, add the one narrow proof, and leave a short diagnosis so recurrence is trivial to spot.
- Generalize after real repetition, or when the next shared use is known and concrete. An abstraction with one caller is waste. Don't invent a framework for an imagined future.
- Measure real things. The actual process, stored row, browser output, deployed state. Not a stand-in, cached copy, or dashboard flag. Repository text alone proves nothing is dead or working; check the databases and dashboards too.
- Data lives on the thing it describes. A variant declares its own facts where it's constructed; shared systems read them. Question anything that centralizes unrelated decisions (one manager scoring everything, one writer, one mode switch).
- Typed outcomes for distinct realities. Success, failure, unavailable, interrupted, and invalidated are not interchangeable empties. Never infer state by scraping text or exit codes.
- Nothing gets orphaned, silently dropped, or marked terminal before its side effect is confirmed. One owner and one authoritative lifecycle decide the state. Silence is a failure mode.
- Never write "couldn't run the measurement" down as "the experiment failed". Keep the missing-measurement cause distinct and recoverable.
- Delete finished one-off jobs. A completed migration script left in the tree is a loaded gun. Net-negative PRs are good.

## Configuration

- Dials live next to the rules they tune, in small modules. If a value is definitional (0, 1, "always"), it's code, not config.
- Defaults make the intended path work. Don't expose knobs that let a caller, provider, or stale environment choose a system invariant.
- Centralize genuinely shared, user-visible budgets in one named owner. Don't centralize merely equal numbers; app-local policy stays local.
- No config bureaucracy and no standing style nags. On-demand audits and structural gates that protect a real invariant are good. Lint tripwires about taste are not.

## Process

- Fix real problems before building features. Reproduce bugs end to end as a user first, and test the real user path before claiming a cause or a fix.
- Small, specified fixes get done directly. A worker costs 30-45 minutes for any size change; dispatch only work that is genuinely long, heavy, or parallel.
- Batch the work: read everything needed once, then write code, tests, and run the suite in one go. Fewer roundtrips, fewer tokens. Prove prerequisites cheaply before an expensive end-to-end run; a long series of dry runs is not discovery.
- Reviews match blast radius. Kernel, schema, and seam changes get an independent adversarial review. Small fixes get one probe of the real risk plus one test that fails without the fix.
- Every test says why it exists, especially regression guards. Test the invariant that prevented the incident, including timing and recovery boundaries. Never weaken or skip a test to look green; refactors ship with tests moved, never weakened.
- Run the local checks the change's risk actually warrants before pushing. CI confirms; it never discovers. Measure before adding process.
- Squash-merge, never push main directly, review PRs to zero blockers. Keep PRs draft after local gates; the reviewing manager flips ready once the exact head is merge-worthy.
- Before saying "fixed", check every item raised and state the remaining blocker plainly.
- Preserve completed work through a coherent checkpoint. After an interruption, continue from it; never blindly regenerate or discard valid state.
- If something looks off inside the change you're making, fix it. Same for red tests, lint, and flakes in that scope. Outside your scope, file or flag it; never quietly bundle it in.

## Docs and voice

- Docs are a short, human-readable judgment surface. Lead with what and when. Changing specifics live in code, tests, issues, and memory.
- If it reads like slop, rewrite it as what I'd actually say. Sentence case. No em dashes. Judge animation and feel from footage, not adjectives.

## UI

- Build around the interaction that carries the rules. A 3D scene is the hero only when the scene is how you read the game; never default to 3D because it looks good.
- Start from the user's model of the product. Stable things stay stable; move the camera or focus when that is what the user meant.
- One fact per surface, labeled. No duplicated information anywhere; the logo twice is a bug. Layout is hierarchy and attention direction; if an element doesn't help, cut it.
- The control lives where the decision happens (the card is the button). Prefer direct visible choices over generic buttons and hidden state.
- Icons come from the lucide pipeline only, never hand-authored SVG. One glyph per control, colour working inside the glyph with a contrast floor.
- Latency is product quality. Set and test budgets for user-visible interactions in the existing harness.
- Inspect the rendered result. A screen that works but looks wrong, overlaps, or hides the interaction is not done.

## Orchestration

- Multi-slice work runs through SightMesh: sol manages, terra/luna execute, one program prefix per worker, disjoint file ownership, durable handoffs, the manager reviews and merges. The coordinator writes briefs, gathers evidence, deliberates with me, oversees merges, and deploys dev.
- Don't coordinate with finished workers or nest orchestration. Read the diff, continue from the checkpoint. Report inherited blockers with the exact evidence.
- Never invent a release flow, CLI, endpoint, identity, or policy from a plausible name. Verify it in live code and state first.

## Tools and safety

- Shell-safe by default: no backticks inside double-quoted bodies; use body files or single-quoted heredocs.
- Secrets never print, only their shape. Anything that appeared in a transcript is exposed; rotate it.
- If you're testing on my machine, don't take over my screen. Headless only.

## Models and cost

- Route by cognitive risk: Fable plans and reviews kernel-class changes; sol orchestrates; terra implements and audits; luna does bounded mechanical work. GPT (codex) and Claude accounts both exist; fail over on quota or auth errors only, never on test failures.
- Fewer roundtrips, fewer tokens. A 15-minute direct fix beats a 45-minute worker round trip.
