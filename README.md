# variapt

Compact nesting PoC around unchanged [Sparrow](https://github.com/JeroenGar/sparrow).
Stages 1–6 are implemented in two Python files: solver/Mubit orchestration in
`poc.py`, and geometry, expansion, checks and pictures in `geometry.py`.

Project-specific code is written during the event. Open-source libraries and our
own tooling may be reused. **Every project LLM call goes through Mubit.** Only
`episode` and `demo` without `--mock-action` call a model, once per invocation.

## Setup

```bash
python -m pip install -r requirements.txt
```

The existing stock binary at `.cache/sparrow-target/release/sparrow` is reused.
Set `SPARROW_BIN` or pass `--sparrow /path/to/sparrow` to use another build.
For a fresh checkout, build the same upstream revision with Rust 1.90 or newer:

```bash
mkdir -p .cache
git clone https://github.com/JeroenGar/sparrow.git .cache/sparrow
git -C .cache/sparrow checkout 1fe3e3146110c34d77510636defb737c7ce893c6
CARGO_TARGET_DIR="$PWD/.cache/sparrow-target" cargo build --release --manifest-path .cache/sparrow/Cargo.toml --features only_final_svg --bin sparrow -j 2
```

Use a fresh output directory for each command. `--seconds` caps search time per
solve; standalone `run` and `group` add startup/export overhead. Episodes also
have one total `--budget-seconds` limit (default 30). Defaults use two workers,
capped at four. An episode runs a baseline and one chosen action. A pair action
also refines the released original parts while time remains. Standalone
`group --refine` runs a grouped solve and a refinement.

## Stages 1–2: solve and sample pairs

```bash
python poc.py run examples/repeated.json --out runs/plain --seconds 2
python poc.py pairs examples/repeated.json --out runs/pairs --keep 4
```

`run` saves native `solution.json`, numbered `layout.png`, native `layout.svg`,
`solver.log`, `validation.json` and metrics in `run.json`. Native solution JSON
is also accepted as input for warm starting. Utilization uses original-part area.

`pairs` samples up to four legal rotations and twelve buffered vertices per copy
of a repeated concave type (or `--item ID`). It rejects overlap and insufficient
clearance, then keeps compact envelopes that fit the strip. Reflections are not
sampled. `pairs.png` previews the original members and convex envelopes;
`pairs.json` stores local poses, legal group rotations, clearance and counts.
Candidates are ranked by the whole-job grouped-area lower bound, including all
other parts and leftovers; the partial shelf estimate is only a tie-breaker.
When an incumbent is available, grouped formulations whose area bound cannot beat
it are removed before Mubit sees them. Area inflation, required utilization and
length at the baseline's utilization are exposed alongside the bound. These are
diagnostics rather than predicted gains. Deduplication uses actual normalized
envelope geometry, local member poses and rotation permissions.

## Stage 3: grouped solve and expansion

```bash
python poc.py group examples/tiny.json --out runs/grouped --candidate pair_0 --seconds 2 --refine
```

The mixed example has five L-shaped parts and three rectangles. Two pairs replace
four Ls; one L and all three rectangles remain individual parts. Each envelope has
only rotations legal for both members, with reflection disabled. Candidate IDs
come from the same `pairs` command with the same `--item` and `--keep` options.

Outputs include `mapping.json`, the grouped native solve in `solver/`, and
`expanded.json` with original-part poses and original-area density. The final
`solution.json` and numbered `layout.png` always contain original parts. Optional
`--refine` warm-starts Sparrow on the expanded layout in `refine/` and keeps the
shorter valid result. A failed refinement preserves the valid expanded seed.
Envelope SVGs remain inside `solver/`.

A small Shapely pass checks original inventory, allowed rotations/reflections,
strip bounds, overlap and native clearance before accepting a layout. Each part's
linear tolerance is capped by its own dimensions and area/perimeter ratio, so a
long strip cannot hide tiny parts. Positive intersection area is checked directly,
with tolerance capped at one millionth of the smaller part's area; parts are never
eroded away during validation. `validation.json` records the tolerance and checks.

## Stage 4: one Mubit choice

```bash
# Development: no model request or credential loading.
python poc.py episode examples/repeated.json --out runs/mock --seconds 2 --budget-seconds 15 --mock-action pair_0

# Live: reads MUBIT_API_KEY from the environment or .env.
python poc.py episode examples/repeated.json --out runs/live --seconds 2 --budget-seconds 20 --model-timeout 20
```

The observation includes the current result, original quantities/areas, pair IDs,
envelope sizes, fill, strip fit, whole-job area costs, repeat/leftover counts and
the utilization required for a grouped seed to beat the incumbent.
The model selects one advertised action:

- `plain`: a fresh original-part solve with the next seed.
- `pair`: solve a selected envelope seed, expand its poses, then warm-start Sparrow on the released original parts while time remains.
- `refine`: warm-start the current original-part layout, allowing parts to move independently.

All live requests use the fixed [Mubit gateway](https://docs.mubit.ai/reference/api)
at `https://api.mubit.ai/openai/v1/chat/completions`, with redirects disabled and
no direct-provider fallback. The model defaults to `gpt-6-luna`; use `--model` or
`MUBIT_MODEL` to override it. The request has a 1,024-token completion cap and a
configurable HTTP timeout (default 20 seconds), further capped by the remaining
episode budget; there are no retries.

`decision.json` records the credential-free request/response, Mubit request ID,
validated choice and reason. `observation.json`, `baseline/`, `action/` and
`episode.json` show what was offered and executed. The final layout retains the
better valid result. Model/action failures retain the checked baseline and record
failure; mock decisions are explicitly marked.

## Stage 5: one episode deadline

```bash
python poc.py episode examples/repeated.json --out runs/bounded --seconds 2 --budget-seconds 15 --mock-action pair_0
```

One monotonic clock starts when the episode begins. Input loading, baseline,
sampling, Mubit, the chosen solve, geometry checks and plotting all consume the
same budget. Solver search time shrinks to whole seconds that fit, with a small
export reserve; the episode has no extra five-second solver allowance. The model
observation includes remaining time and the per-solve search cap.

A POSIX timer interrupts blocking work, including HTTP response reads. A timed-out
Sparrow process is killed and reaped. A candidate replaces the incumbent as soon
as its geometry checks and data export finish before the deadline, if shorter.
Pictures run afterward and cannot undo acceptance. Checked seeds also survive
subsequent refinement failures or timeouts. Missing pictures are reported as null.
Failed, invalid, late or longer candidates preserve the already checked result.
After stopping, the runner copies that result and any complete existing picture to the
root output; it performs no fresh geometry checks or plotting. Small cleanup and
log-writing overhead is reported in `elapsed_seconds` and `budget_overrun_seconds`.

`episode.json` records the budget, acceptance time, selected result and
`candidate_status` (`accepted`, `not_shorter`, `rejected`, `late` or `not_run`).
Exit codes are 0 for completion, 1 for failure and 2 for `budget_exhausted`.
If the budget cannot establish a checked baseline, `solution` is null and no
final solution is claimed. Once established, its files survive later failures.

## Stage 6: real non-convex parts

```bash
python poc.py demo --out runs/demo --seconds 5 --budget-seconds 30 --model-timeout 15
```

The default input is the full [GARDEYN4 benchmark](examples/gardeyn4.json): **80
parts of five non-convex types**, including detailed curved outlines with over
1,000 vertices. It comes from the [authors' industrially derived benchmark
family](https://arxiv.org/html/2509.13329v2#S12), using AlmaCAM metalworking and
shipbuilding shapes. Original coordinates, quantities, rotations and strip height
are unchanged. Native separation is zero; physical units are unspecified.
[Source, hash and license](examples/README.md) are included beside the input.
Pass another native job as the optional positional argument to `demo`.

In addition to the episode outputs, `demo` writes `comparison.png` with original
before/after layouts at the same scale, plus `demo.md` containing the executed
baseline/action/final metrics, model reason/request ID, pair previews and source
links. Report generation consumes the same total deadline. Dense layout labels
use smaller text, and repeated polygon erosion is cached within each validation.

The historical pre-fix live run in `runs/stage6-live/` made **one real Mubit call** and
completed in **27.474 seconds** under its 30-second limit:

| Executed result | Strip length | Original-area utilization |
| --- | ---: | ---: |
| Stock baseline | 5229.594700 | 72.34% |
| Mubit-selected `pair_0` | 6420.026400 | 58.93% |
| Retained final layout | 5229.594700 | 72.34% |

All 80 original parts passed checks. Mubit chose `pair_0` using its partial paired
length estimate; the executed whole-job result was longer, so the baseline was
retained. **Measured gain: 0.00%.** Conservative pair envelopes reserve their empty
interiors, limiting their usefulness in this mixed job. The actual trace is in
`runs/stage6-live/decision.json`; the full report is `runs/stage6-live/demo.md`.

## Corrections after review

Whole-job cost ranking/filtering, geometry-and-pose deduplication, part-scale
overlap validation, acceptance before plotting, and automatic released-part
refinement are implemented. Pair candidate IDs can change with the corrected
ranking. Model requests explicitly distinguish grouped-seed bounds from released
refinement and forbid comparing a partial shelf estimate with the full layout.

```bash
python poc.py demo --out runs/corrected --seconds 2 --budget-seconds 20 --mock-action pair_0
```

The single corrective smoke run in `runs/fixes-poc/` validated all 80 original
parts and completed pairing plus refinement in 13.418 seconds. Length changed
from its own two-second baseline, 5435.844700, to 5296.473000 (**2.56% shorter**).
The grouped seed alone was longer at 5934.376000; the released refinement supplied
the improvement. The report shows each phase separately. This used a mock choice,
with no new model calls. The earlier five-second baseline remains shorter at
5229.594700; this smoke does not establish an equal-budget or live-Mubit gain.

Focused local checks reproduced the reviewed overlap, deduplication and plot-timeout
cases, and confirmed that a failed refinement preserves a checked paired seed.

## Completion checks

| Stage | Goal required before proceeding | Verified evidence |
| --- | --- | --- |
| 3 | Grouped solve expands to exact original counts, preserves an odd leftover and other types, passes geometry checks, and warm-starts stock Sparrow. | `runs/stage3-group/`: 2 pairs, 1 leftover L, 3 rectangles; validation passed; refinement log confirms warm start. |
| 4 | One real Mubit decision uses advertised IDs, has a gateway request ID, executes its solver action, and saves a checked final original-part layout. | `runs/stage4-live/`: one `refine` decision and completed warm start; `decision.json` and `episode.json` contain the trace. |
| 5 | One shared deadline bounds work; timeout stops blocking work; invalid, late and longer candidates preserve the checked incumbent; an insufficient budget cannot claim a solution. | `runs/stage5-normal/`, `stage5-late/`, `stage5-invalid/`, `stage5-model-timeout/` and `stage5-short/`. Fault cases use local stubs. |
| 6 | Use sourced real non-convex shapes, execute one live Mubit choice under the total budget, export checked original-part comparisons and report actual gains or zero honestly. | `runs/stage6-live/`: full GARDEYN4, 80 validated parts, one recorded live decision, comparison/report and zero measured gain. |

**All six stages are complete.** A corrected mock-choice episode improved its own
baseline; an optimization benefit from live Mubit selection remains unproven.
Verification used short smoke runs and one live real-shape demo: no ablations,
solver sweeps or regression suite. See the [compact plan](plan_24h.md) for the
completion evidence.
