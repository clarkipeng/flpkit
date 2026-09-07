# Taste

How we choose what to build and how to work. Read the repository's `AGENTS.md` for its commands, workflow, and safety rules.
Shared guidance maintained in the private taste-corpus repository. Repository-specific additions follow below when needed.

## How to work with me

- Do the requested work without asking permission for each ordinary step.
  Ask when I need to choose the product direction, approve spending or an irreversible action, or decide what to leave out.
  Recommend an option and explain its cost; don't ask "should I continue?".
- "ok", "go", "yes", and "continue" mean proceed with the proposed work.
  "Wait" means stop. Asking for an explanation does not authorize a change.
- Keep me updated before I have to ask: what changed, what's running, what's next, and what needs me.
- Speak plainly. Say what's done and what's still needed, without ceremony.
- Explain what happens today, what's wrong, and what you would change.
  Show a small before/after example when the design is hard to follow, and explain why each added part is needed.
  If I ask "wdym", use a concrete example instead of repeating the same explanation.
- Put the result in the reply or link something I can open.
  When I need to decide, show the choices, your recommendation, and what we give up.
- Keep a short, current status document: now, waiting on me, done, known issues.
- Report what actually happened. If an earlier claim was wrong, correct it everywhere you repeated it.

## Architecture

- Use the simplest design that meets the requirements.
  Every added part should prevent a real failure or support a known use. Explain what it reuses or replaces.
- Prefer one rule that prevents a class of mistakes over a growing list of special cases.
  For example, prevent duplicate records in the database instead of checking for duplicates in every caller.
- Build reliable safeguards before adding systems that guess what happened or sort failures into categories.
  Add those systems when real examples show what they need to do.
- Keep one implementation of each capability. When replacing it, remove the old path in the same change.
  Keep old behavior only when supported users or saved data still depend on it.
- Fix the cause, not the symptom. Find how the failure happened, prevent it, add a focused test, and leave a short explanation.
  Don't hide it with an arbitrary limit or a temporary workaround.
- Share code when uses actually repeat, or the next use is already known. Don't build a framework for imagined future needs.
- Check what users actually see, what the process does, or what the database stores, not just what code or a dashboard suggests.
  Keep the original evidence of what happened, where, when, and why; derive summaries and counts from it.
  Organize it enough to find and check later.
- Keep information with the thing it describes. Shared code should read that information, not maintain a second list of facts about every variant.
- Record distinct outcomes explicitly: succeeded, failed, unavailable, interrupted, or no longer valid.
  Don't turn them all into an empty result or guess them from log wording or a generic exit code.
- Give each job an owner responsible for completion and recovery. Don't lose work silently or mark it done before its actual result is confirmed.
- "We couldn't run the measurement" is not "the experiment failed". Record why it couldn't run so it can be resumed.
- Remove one-off scripts once their job is finished. Leaving them runnable risks repeating an operation that should happen only once.

## Configuration

- Put a setting beside the behavior it controls. Don't make a fixed rule configurable.
- Defaults should make the normal use work. Settings must not let callers bypass correctness or safety rules.
- Give a genuinely shared limit one definition. Equal numbers with different purposes do not need to share a setting.
- Automate checks that prevent real mistakes, not checks that nag about taste or add paperwork.

## Process

- Fix real problems before adding features. Reproduce a bug through the user's actual steps before claiming its cause or a fix.
- Match review effort to what could break. Changes to shared core logic, stored data formats, or connections between components need an independent reviewer who tries to find failures.
  Small fixes need a focused check of the risk and a test that fails without the fix.
- Explain why each test exists. Cover the rule that prevents the bug, including timing or recovery when relevant.
  Don't weaken or skip tests to get a passing result, including when reorganizing code.
- Before setting evaluation rules, show a real example from start to finish. Choose what to check and how much error to allow from the claim being tested.
  Separate "followed the rules" from "performed well": a model that makes legal but weak moves may still be useful as a starting point for training.
- Run the relevant local checks before pushing; automated checks on the server should confirm that evidence.
  Measure the problem before adding more process.
- Before saying "fixed", check every issue raised and clearly name anything still blocked.
- Save completed work so it can be resumed after an interruption. Continue from it instead of starting over or discarding valid results.
- Fix problems within the change you're making, including failing or unreliable tests. Report unrelated problems; don't quietly bundle them in.

## Docs and voice

- I should be able to understand every instruction in TASTE or AGENTS without translating jargon.
  Say what to do and why. Use a small example when needed; don't shorten wording at the expense of meaning.
- Keep docs short and current. Lead with what changed and when; link to changing details instead of copying them.
- Write each rule in one place and link to it. TASTE explains how to choose; AGENTS explains how to work in this repository.
- Use plain language, sentence case, and no em dashes. Show animation and interaction in footage instead of describing how good they feel.

## UI

- Build around the interaction that helps users understand the product. Use 3D when it helps explain or play the game, not just because it looks impressive.
- Match how users expect the product to behave. Keep stable objects in place; move the view or focus when that's what the user intended.
- Show each fact once, clearly labeled. Use layout to show what matters; remove elements that don't help.
- Put controls where users make the decision. If selecting a card is the action, make the card clickable rather than adding a separate button.
- Use the product's existing icon set. Keep controls recognizable and readable.
- Set response-time limits for user actions and test them with the existing tools.
- Look at the rendered screen. Working code is not enough if elements overlap, look wrong, or hide the interaction.

## Orchestration

- Follow the repository's workflow. Give each result one owner; agents working in parallel should edit separate files and leave enough notes for another agent to continue.
  Put model names and tool choices in AGENTS or the guide it links to.
- Don't keep managing finished agents or add layers of managers. Read their changes, continue from saved work, and report remaining blockers with evidence.
- Check that a command, service address, account, or release procedure actually exists before using it. Don't invent one from a plausible name.

## Tools and safety

- Never print secret values. Show only safe details, such as whether a key is present; treat a leaked key as exposed and arrange its replacement.
- Tests must not take over my screen. Run automated browser tests without opening visible windows or moving my mouse.

## Efficiency

- Minimize the total tokens, time, and cost needed to finish correctly, including reading context, coordination, review, and fixing mistakes.
  Use more agents or a different model only when the expected benefit pays for that overhead.
- Read the relevant changes, not the whole history. Group independent searches and reads; reuse results that are still valid.
  Keep repeated context stable so it can be cached. Hand over decisions, exact paths, evidence, and blockers, not the whole conversation.
- Give each agent a separate result to deliver. Add a manager only when the work actually needs coordination.
- Wait for completion notifications instead of repeatedly asking "done yet?". Check when new information, a deadline, or a stuck task calls for action.
- Before an expensive check, know what it will prove and confirm the cheap prerequisites first.
  Reuse passing checks until relevant code or assumptions change. Save tokens by cutting unnecessary work, not safety, correctness, or clear explanations.
