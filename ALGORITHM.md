# Architectural review

The original premise was too strong: detailed model analysis does not automatically
improve a mature non-convex packing optimizer. The controls must express useful
hypotheses, and those hypotheses need feedback from the complete problem.

## Substantiated defects

- **Wrong geometry.** We replaced non-convex relations with convex hulls. One
  observed comb pair has original area 130,000 but a 155,034.32-area hull: 19.26%
  extra material per pair. That reserves precisely the cavities the model notices.
- **Wrong optimization space.** Running search on rigid compounds spends the
  budget fixing artificial constraints. Releasing afterward does not recover that
  time. Multiple subprocesses also restart optimizer state and add import/export work.
- **Weak feedback.** Legal poses and low area inflation do not establish that a
  recipe improves global construction, much less the final optimized layout.
- **Wrong runtime role.** Model calls on every solve add substantial latency.
  A reusable recipe can amortize synthesis; a known losing recipe should not be
  deployed repeatedly.
- **Misleading credit.** Improvement over an unoptimized initializer includes
  ordinary Sparrow progress. It is not evidence of model advantage.

A direct world-space repair prototype was also checked. Its two real proposed
pockets were blocked, with zero direct gain (`runs/direct-repair-live/`). The
local repair code was removed. The final approach asks the model for relations
between shapes and lets existing Sparrow construction handle global placement.

## Implemented flow

1. Construct a checked original-part baseline with native `--global-time 0`.
   Existing solution input is checked and retained instead. No optimization time
   is required for this step by default.
2. Send full original vertices, permissions, poses, neighbours, strip occupancy,
   logs and an image through Mubit. Approximate contour/cavity views aid analysis;
   all execution still uses the original vertices.
3. `inspect_patterns` measures batches of observed clusters or NEW relative
   poses. Each member uses a type ID, rotation, reflection and bounding-box anchor;
   the compiler handles original coordinate origins and checks internal clearance.
4. Compile a conservative **non-convex** outline. Thin bridges connect members;
   open cavities remain available. Closed holes are filled because native items
   cannot contain holes, and their cost is exposed. Footprint, hull fill, bridge
   length and whole-job inflation remain diagnostics, not predictions.
5. `finish` validates a combined multi-pattern recipe against the exact inventory,
   runs native construction with optimization disabled, expands original parts,
   and measures the COMPLETE initializer. A worse initializer returns its actual
   layout to the model for correction. Invalid recipes also remain correctable.
6. Run ONE continuous native optimization on independent ORIGINAL parts, using
   the entire remaining search allowance. Compounds never consume timed search.
   An absent, rejected or failed model recipe uses the stock path. Keep the
   shortest checked layout if later work fails or expires.
7. Save accepted initializer recipes with the source-job fingerprint and real
   Mubit provenance. Replay rechecks geometry and initialization without model
   calls. An explicit rejected end-to-end evaluation bypasses the recipe entirely.

Generation's acceptance check measures initialization only. End-to-end rejection
uses an explicit comparison of recorded final results; the latest recipe contains
that evidence. A geometric or initializer check is never a final-performance certificate.

`--seconds` is a shared native search allowance. `--budget-seconds` independently
bounds model time, construction, search, checks and plots. Analysis leaves time
reserved for native search. Startup and construction CPU time are additional to
nominal search; they remain inside the wall deadline. No Rust patch or solver
rewrite is involved.

## What the latest evidence establishes

Two real Mubit calls synthesized opposite-facing disk pairs and interdigitating
comb pairs from explicit relative poses. The complete initializer changed from
**6096.903 to 5838.202**, a **258.701-unit / 4.24% improvement before optimization**.
All compounds then expanded, and the full 30-second search used the original 80
parts. The complete compilation/run took 77.825s, including 38.800s of model calls.
A short replay reproduced the initializer exactly with no fresh model call.

The final layout was **5094.6406**, versus the earlier same-job, two-worker,
30-second stock reference of **5050.2050**: the candidate was **0.88% longer**.
That recipe is marked rejected for future replay. This is a concrete example
where a better initializer did not produce the better final result. One observed
comparison does not isolate basin effects, stochastic variation or establish
repeatable performance. An overall model-assisted optimization advantage remains
unproven; the code and report do not claim one.

Focused checks cover true-contour containment, novel mixed patterns, explicit
closed-hole costs, inventory/reflections, rejection feedback, model-free replay,
failed-model native fallback, and preservation of the search allowance. No sweep
or ablation campaign was added. See the
[live report](runs/pattern-feedback-live/demo.md),
[comparison data](runs/pattern-feedback-live/comparison-stock.json), and
[real recipe with rejection evidence](runs/pattern-feedback-live/recipe.json).
