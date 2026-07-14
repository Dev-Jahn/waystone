---
name: brain-tempest
description: This skill should be used when the user runs "/jahns-workflow:brain-tempest", has a new or half-formed project idea, says things like "I want to build X but haven't thought it through", "help me figure out what I'm actually building", "let's scope/shape this project", "brainstorm the direction", or asks to "write a design doc / SSOT" — especially at the very start, before any design document exists. It draws a project's north-star out of the user through Socratic questioning and writes it as a ready-to-adopt SSOT.md that the rest of the workflow anchors to.
argument-hint: "[one-line project idea] (optional — I'll ask if omitted)"
---

# jahns-workflow: brain-tempest

The ideation front door. Turn a one-line, half-formed idea into a **north-star `SSOT.md`** by
questioning it: sharpen the vision, surface the decisions the user hasn't made yet, decide the
obvious things yourself, and ground the rest with research. The whole workflow is SSOT-anchored,
so this is where the anchor is forged.

Runs before everything else — **no git, no `.jahns-workflow.yml`, no prior structure required**.
It closes a real gap: without it, `/jahns-workflow:init` faced with no design doc can only scaffold
an *empty* SSOT.md, leaving an SSOT-anchored project with a hollow anchor. You fill it.

Plugin root = two directories above this skill's base directory. The authority on what an SSOT is
and how it is consumed: `<plugin-root>/references/conventions.md` §4.

## The one thing you're producing

A single `SSOT.md` at the project root, at **north-star altitude**. A north-star sets direction and
catches drift; it is not a spec. That distinction is the whole job:

- **Capture**: the problem and why-now, a one-line vision, the primary user and the job they're
  hiring this for, the guiding principles, the hard constraints, the explicit **non-goals**, the
  few big directional bets — and the **open questions** you couldn't (or shouldn't yet) resolve.
- **Leave out**: architecture, tech-stack minutiae, exhaustive feature lists, schemas, API shapes.
  Those are downstream decisions (ADRs, `decision/` tasks). Writing them here is false precision —
  it reads as settled when it isn't, and the project drifts the moment reality disagrees.

A machine needs clearance to run without seizing; an SSOT needs the same slack. When you are unsure
whether something belongs, go higher. Under-specified-but-directional beats precise-but-wrong.

## The loop

You are having a conversation, not administering a form. Keep it short and sharp.

1. **Seed.** If the argument carried a one-liner, use it. Otherwise ask one plain, tiny question —
   *"If you had to describe this project in one sentence?"* — and nothing more. Never open with a questionnaire.

2. **Grow the tree — silently.** From the seed, branch out *in your own reasoning* (shown to no one,
   saved nowhere) the things that would have to be pinned down to write a real SSOT: who it's for,
   what it refuses to be, the core tension, the bet that makes it interesting. Go a little at a time
   — one or two branches deep — not an exhaustive dump. Each answer reshapes the tree, so there is
   no point mapping it all up front.

3. **Prune before you ask.** A branch is *not* worth a question if it is trivial, has an obvious
   better default, or a few minutes of web research would settle it — **decide those yourself**
   (silently run a **quick** research pass with a background subagent when it helps). Spend the user's attention only on genuine forks: places where the
   project could credibly go several ways and only their taste or values break the tie.

4. **Ask maieutically** (next section). One question per turn — occasionally a small cluster of
   truly independent ones. Because each answer reshapes the tree, don't commit to a fixed list.

5. **Mirror.** After each answer, say back in one line what you think you heard —
   *"So the north-star is X, and Y is explicitly not the point — right?"* This confirms you read
   them correctly and lets them correct you for free. It is the cheapest error-correction you have.

6. **Stop when it's enough.** Enough = you can write every section at north-star altitude with
   confidence, and no remaining fork would change the project's *direction* (only its details, which
   don't belong here anyway). Aim for roughly 3–6 exchanges. If you get there and something is still
   open, **don't drill** — write it into the SSOT as an open question. A named uncertainty is an
   asset; a worn-out user is not.

## Asking maieutic questions

`AskUserQuestion` is normally a picker: you hand over answers and the user chooses one. Invert it.
Your options are not answers — they are **framings**, three sharply different ways of seeing what
this project *is*, each with a consequence. Laid side by side they force the user to notice where
they actually stand, and that noticing is the point.

The signal you are after is **not which chip they click**. It is which framing they lean toward *and
what they say in their own words* about why. So:

- Write each option as a small thesis with a "therefore," not a bland label. Make them genuinely
  rival worldviews, not three intensities of the same one.
- In the question, ask them to pick the closest **and add a line on why — or on what's off about
  it** — and tell them that line is where the real answer lives. "Other" and free-text notes are
  wins, not detours.
- Read the reply as *intent to interpret*, then mirror it (step 5). Never just log "chose B."

**Example.** Seed: *"a dotfiles manager CLI for developers."* A weak question asks "what features do
you want?" A maieutic one asks what the tool is *for*:

> **If you had to narrow this tool's reason for existing down to one thing, which comes closest?**
> (pick the closest, but add a line on *why* it feels that way — or what's off about it — the real
> signal is there)
> - **Reproduce** — boot my environment onto a new machine, losslessly. Dotfiles are a deployment artifact.
> - **Understand & evolve** — trace why my config is the way it is and experiment safely. Dotfiles are a living codebase.
> - **Share** — good defaults for others to steal. Dotfiles are an open-source product.

Those are not three features — they are three *different products* with different north-stars (an
ops tool, a personal-knowledge tool, a community artifact). Whichever the user leans toward, plus
their one line, tells you which SSOT you are about to write. And listen past the pick: *"Reproduce,
but honestly I keep tweaking things and losing track of why"* points at **Understand & evolve** —
you just learned the real north-star from the aside, not the choice.

## Writing the SSOT.md

When you have enough, write `SSOT.md` at the project root — the git top-level if you're in a repo,
otherwise the current directory. If an `SSOT.md` already exists, read it and deepen it rather than
clobbering, or confirm before replacing (this workflow never silently overwrites the user's docs).
It must feed the SSOT tooling (`ssot.py`), which splits the file on level-2 `##` headings and
injects each section's **first paragraph** as the per-session digest. The structure is therefore
load-bearing, not cosmetic:

- `# <Project name>` title, then a handful of `##` sections with headings a newcomer can navigate.
- **Every `##` section opens with one crisp paragraph** stating its essence — that paragraph is what
  every future session reads first. Detail comes after it, never before.
- Write claims that could later be proven wrong (conventions §4: *binding but falsifiable*). A
  falsifiable north-star is what lets contrary evidence trigger a `decision/` → ADR later instead of
  silent drift. A vague affirmation can't be contradicted, so it can't guide.
- Where a concrete fact sharpens the vision — a real constraint, a market reality, a hard number —
  **research it** rather than hand-wave.

A skeleton to calibrate altitude and the opening-paragraph rule. Adapt the sections to the project;
do not fill it in like a form:

```markdown
# <Project>

## Problem & why now
One paragraph: the pain, who feels it, why it is worth solving now. Specifics after, if useful.

## Vision
One line someone could repeat back. Then what it means — and, crucially, what it does **not** mean.

## Who it's for & the job
The primary user and the job they are hiring this for. Secondary users only if they change decisions.

## Principles
The handful of values that break ties when the roadmap turns ambiguous. Each should be able to say
no to something otherwise tempting.

## Non-goals
What this deliberately is not and will not do — the boundaries that keep scope from sprawling.

## Bets
The few directional wagers the project is making (approach, audience, tradeoff), named so they can be
revisited when evidence lands.

## Open questions
What is genuinely undecided. This section is a feature, not an admission — it is where the clearance
lives.
```

If you find yourself writing an interface or a data model, you have dropped too low: that is a
downstream decision, not the anchor. Climb back up.

## Handoff

Leave `SSOT.md` **uncommitted** for the user to read. Show a tight summary — the vision line, the
section map, and any open questions you recorded — then point them at the next step:

> `/jahns-workflow:init` — it detects `SSOT.md` as the SSOT and scaffolds the harness around it.

Don't run init yourself; it has its own decisions to walk through (review mode, existing structure).
Respond in the user's configured language throughout.
