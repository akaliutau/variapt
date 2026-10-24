# variapt

A compact PoC using Mubit to synthesize geometric initialization recipes for
unchanged [Sparrow](https://github.com/JeroenGar/sparrow). The model proposes
relationships between original parts; exact geometry and native construction
check them. All timed optimization runs on independent original parts.

Project code is written during the event. Open-source libraries and existing
tooling may be reused. **Every project LLM call goes through Mubit.**

## Run

```bash
python -m pip install -r requirements.txt
```

Reuse `.cache/sparrow-target/release/sparrow`, or set `SPARROW_BIN` / `--sparrow`.
For a fresh checkout, build the pinned upstream revision with Rust 1.90 or newer:

```bash
mkdir -p .cache
git clone https://github.com/JeroenGar/sparrow.git .cache/sparrow
git -C .cache/sparrow checkout 1fe3e3146110c34d77510636defb737c7ce893c6
CARGO_TARGET_DIR="$PWD/.cache/sparrow-target" cargo build --release --manifest-path .cache/sparrow/Cargo.toml --features only_final_svg --bin sparrow -j 2
```

Put `MUBIT_API_KEY` in the environment or `.env`. Use fresh output directories:

```bash
# Synthesize, check and evaluate a recipe, then optimize original parts.
python poc.py demo --out runs/compile --seconds 30 --budget-seconds 120

# Reuse an emitted recipe, with no new LLM calls.
python poc.py demo --out runs/replay --recipe runs/compile/recipe.json --seconds 30 --budget-seconds 60
```

A recipe is emitted only after it improves the complete checked initializer.
Replay rechecks its source fingerprint, internal geometry, inventory and global
initialization score. A recipe marked rejected by an end-to-end comparison is
bypassed in favour of stock. Compilation cost remains in its provenance; replay
is not a new real model call or a free compilation claim.

`demo` uses full GARDEYN4: **80 parts of five real non-convex types**, with original
coordinates, counts, rotations and strip height. Separation is zero and physical
units are unspecified. [Provenance, hash and license](examples/README.md) accompany
the input. Pass another native job or checked solution as the optional positional
argument. `episode INPUT` runs the same flow without the comparison report.

## Architecture

The [review](ALGORITHM.md) explains the fundamental defects in the previous design:
convex envelopes erased useful cavities, rigid-compound optimization spent search
on a proxy problem, and geometric feasibility was mistaken for global usefulness.

The model now receives all vertices and permissions, the actual layout, contacts,
occupancy, solver logs and an image. Approximate contour/cavity views help it
interpret dense curves. `inspect_patterns` checks observed or newly synthesized
relative arrangements, with actual original-part geometry. Bounding-box anchors
remove coordinate-origin arithmetic from the model's task.

The compiler retains non-convex contours, joins disconnected members by thin
bridges, and exposes closed-hole fill costs. `finish` tests the whole recipe using
native construction with **zero optimization time**. Worse initializers return
actual layout feedback for revision. The selected seed expands immediately; one
continuous native solve gets all remaining search time on the original parts.

Three Python files implement this: `planner.py` for Mubit/tool conversation,
`geometry.py` for patterns/checks/pictures, and `poc.py` for native execution,
acceptance and recipe replay. Sparrow is unmodified.

Live requests use the fixed Mubit route
`https://api.mubit.ai/openai/v1/responses`, with redirects disabled, no retries
and no direct-provider fallback. Default: `gpt-6.1-sol`, medium reasoning, three
model turns, 8,192 output tokens and a 60-second per-request cap. Override with
`--model`, `MUBIT_MODEL`, `--reasoning-effort`, `--planner-turns`,
`--max-output-tokens` and `--model-timeout`. Stateless continuation preserves all
response items and tool feedback with `store: false`.

## Budgets and acceptance

For episodes, `--seconds` is one native search allowance across all optimized
calls (default 30). `--initial-seconds` defaults to zero: native construction only.
`--budget-seconds` separately caps complete wall time (default 120). Model calls,
construction, startup, checks and plots share that deadline. Native work is
reserved if model analysis runs out of time. Two workers are used, capped at four.
Standalone `run` and `group` keep a per-solve search cap.

Inventory, legal poses, bounds, overlap and clearance are checked with part-scale
native f32 tolerances. Accepted data precedes optional plotting. A failed/invalid
or late result cannot replace the checked incumbent. Exit codes: 0 completion,
1 failure, 2 wall-budget exhaustion. Model failures are recorded and use native
fallback when time remains. Small cleanup/export overhead is reported.

Outputs: `episode.json`, retained `solution.json` / `validation.json` / `layout.png`,
`baseline/`, every native construction/solve in `trials/`, `patterns.json`, and
an eligible `recipe.json`. Live `planner/` contains full context, credential-free
request/response traces, real gateway IDs and readable `analysis.md`. `demo.md`
and `comparison.png` show checked original-part layouts. Initializer gains are
reported separately from ordinary native optimization.

## Actual result

The [latest live run](runs/pattern-feedback-live/demo.md) used two real Mubit calls
to generate new disk-pair and comb-pair templates. Its complete initializer was
**4.24% shorter** (6096.903 to 5838.202). All 80 parts were valid; all 30 seconds of
timed search went to original parts. Total time was 77.825s, including 38.800s of
model calls. A one-second replay smoke reproduced initialization without an LLM.

The final result was **5094.6406**, versus **5050.2050** for the existing 30-second,
two-worker stock reference: **0.88% longer**. The recipe is therefore marked
rejected for subsequent replay. A better initializer does not guarantee a better
final search basin. Overall model-assisted advantage remains unproven. See
[comparison data](runs/pattern-feedback-live/comparison-stock.json). Earlier
negative comparisons remain in [the historical report](runs/live-advantage/README.md).

## Focused local checks

```bash
python poc.py demo --out runs/smoke --mock-plan examples/analyst-smoke.json --seconds 3 --budget-seconds 20
python poc.py run examples/repeated.json --out runs/plain --seconds 2
```

Mock plans load no credentials and make no model calls. Focused checks verified
contour containment, novel mixed patterns, closed-hole costs, exact expansion,
reflections, correction after rejected recipes, native fallback, and rejected
cache bypass. No sweep or ablation campaign was run. The original `pairs` and
`group` commands remain geometry diagnostics; their sampled convex envelopes are
separate from the live contour-preserving compiler.
