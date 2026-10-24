#!/usr/bin/env python3
"""Compact Sparrow PoC: solve, sample pairs, solve grouped, or execute one Mubit choice."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

from geometry import check_layout, draw_comparison, draw_layout, draw_pairs, expand_pairs, group_job, layout_metrics, pair_candidates, polygon

ROOT = Path(__file__).resolve().parent
DEFAULT_SPARROW = ROOT / ".cache/sparrow-target/release/sparrow"
MUBIT_URL = "https://api.mubit.ai/openai/v1/chat/completions"


class DeadlineExceeded(TimeoutError):
    pass


def remaining(args, reserve=0):
    left = getattr(args, "deadline", float("inf")) - time.monotonic() - reserve
    if left <= 0:
        raise DeadlineExceeded("Episode budget exhausted")
    return left


def expire(signum, frame):
    raise DeadlineExceeded("Episode deadline reached")


def save(path, data):
    Path(path).write_text(json.dumps(data, indent=2, allow_nan=False) + "\n")


def load_job(path):
    job = json.loads(Path(path).read_text())
    if not isinstance(job, dict) or not isinstance(job.get("items"), list) or not job["items"] or "strip_height" not in job:
        raise ValueError("Use a native Sparrow job or native solution JSON")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", job.get("name", "")):
        raise ValueError("Use a job name containing letters, digits, underscores or hyphens")
    if any(key in job for key in ["margins", "grain_constraints", "specification"]):
        raise ValueError("Use native Sparrow constraints; legacy job extensions are unsupported")
    return job


def solve(job, out, args):
    available = remaining(args, reserve=0.5)
    seconds = min(args.seconds, int(available - 0.25)) if hasattr(args, "deadline") else args.seconds
    if seconds < 1:
        raise DeadlineExceeded("Not enough episode budget for a one-second solve and export")
    binary = args.sparrow.resolve()
    if not binary.is_file():
        raise ValueError(f"Sparrow binary not found: {binary}; see README or pass --sparrow")
    out.mkdir(parents=True, exist_ok=False)
    save(out / "input.json", job)
    command = [str(binary), "--input", str(out / "input.json"), "--global-time", str(seconds),
               "--rng-seed", str(args.seed), "--workers", str(args.workers)]
    started = time.monotonic()
    record = {"job": job["name"], "seed": args.seed, "workers": args.workers,
              "search_seconds": seconds, "command": command}
    try:
        with (out / "solver.log").open("w") as log:
            timeout = remaining(args, reserve=0.5) if hasattr(args, "deadline") else seconds + 5
            subprocess.run(command, cwd=out, check=True, timeout=timeout,
                           env={**os.environ, "RAYON_NUM_THREADS": str(args.workers)},
                           stdout=log, stderr=subprocess.STDOUT)
        remaining(args)
        final = out / "output" / f"final_{job['name']}.json"
        result = json.loads(final.read_text())
        svg = final.with_suffix(".svg")
        if svg.exists():
            shutil.copyfile(svg, out / "layout.svg")
        remaining(args)
        record.update(status="completed", elapsed_seconds=round(time.monotonic() - started, 3))
    except Exception as error:
        timed_out = isinstance(error, DeadlineExceeded) or (hasattr(args, "deadline") and isinstance(error, subprocess.TimeoutExpired))
        record.update(status="budget_exhausted" if timed_out else "failed",
                      elapsed_seconds=round(time.monotonic() - started, 3), error=str(error))
        save(out / "run.json", record)
        if timed_out:
            raise DeadlineExceeded(f"Sparrow exceeded the episode budget; see {out / 'solver.log'}") from None
        raise ValueError(f"Sparrow run failed; see {out / 'solver.log'}: {error}") from None
    save(out / "run.json", record)
    return result, record


def finish(job, result, out, record, args=None):
    remaining(args)
    validation = check_layout(job, result)
    remaining(args)
    save(out / "validation.json", validation)
    save(out / "solution.json", result)
    record.update(**layout_metrics(job, result), validation=validation,
                  solution=str(out / "solution.json"), image=None)
    remaining(args)
    save(out / "run.json", record)
    return record


def render_layout(job, result, out, record, args=None):
    """Presentation cannot undo a checked export; publish only a complete picture."""
    temporary = out / "layout.tmp.png"
    try:
        remaining(args)
        draw_layout(job, result, temporary)
        remaining(args)
        temporary.replace(out / "layout.png")
        record["image"] = str(out / "layout.png")
    except DeadlineExceeded:
        raise
    except Exception as error:
        record["plot_error"] = str(error)
    finally:
        temporary.unlink(missing_ok=True)
        save(out / "run.json", record)
    return record


def run(args):
    job, out = load_job(args.input), args.out.resolve()
    result, record = solve(job, out, args)
    finish(job, result, out, record)
    return render_layout(job, result, out, record)


def write_pairs(job, out, args, incumbent_length=None, preview=True):
    remaining(args)
    result = pair_candidates(job, args.item, args.keep, incumbent_length)
    save(out / "pairs.json", result)
    if preview:
        draw_pairs(job, result, out / "pairs.png")
    remaining(args)
    return result


def pairs(args):
    job, out = load_job(args.input), args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    result = write_pairs(job, out, args)
    return {"item_id": result["item_id"], "candidates": len(result["candidates"]),
            "contacts_tried": result["contacts_tried"], "elapsed_seconds": round(time.monotonic() - started, 3),
            "data": str(out / "pairs.json"), "image": str(out / "pairs.png")}


def grouped_solve(job, candidate, out, args):
    remaining(args)
    out.mkdir(parents=True, exist_ok=False)
    save(out / "input.json", job)
    grouped, mapping = group_job(job, candidate)
    save(out / "mapping.json", mapping)
    result, solver_record = solve(grouped, out / "solver", args)
    expanded = expand_pairs(job, result, mapping)
    check_layout(job, expanded)
    save(out / "expanded.json", expanded)
    record = {"status": "completed", "action": "pair", **mapping, "solves": [solver_record]}
    if getattr(args, "refine", False):
        try:
            refined, refinement = solve(expanded, out / "refine", args)
            check_layout(job, refined)
            record["solves"].append(refinement)
            record["refinement_status"] = "completed"
            if refined["solution"]["strip_width"] < expanded["solution"]["strip_width"]:
                expanded = refined
        except DeadlineExceeded:
            raise
        except Exception as error:
            record.update(refinement_status="failed", refinement_error=str(error))
    return expanded, finish(job, expanded, out, record, args)


def group(args):
    job, out = load_job(args.input), args.out.resolve()
    candidates = pair_candidates(job, args.item, args.keep)
    candidate = next((c for c in candidates["candidates"] if c["id"] == args.candidate), None)
    if candidate is None:
        raise ValueError(f"Unknown candidate {args.candidate}; generate candidates with pairs first")
    result, record = grouped_solve(job, candidate, out, args)
    render_layout(job, result, out, record)
    save(out / "pairs.json", candidates)
    draw_pairs(job, candidates, out / "pairs.png")
    return record


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def choose(observation, out, args):
    """One call, exclusively through Mubit; mock mode never loads credentials."""
    started = time.monotonic()
    trace = {"mode": "mock" if args.mock_action else "mubit", "status": "pending", "request_id": None}
    try:
        remaining(args)
        if args.mock_action:
            action = {"action": "pair" if args.mock_action.startswith("pair_") else args.mock_action,
                      "candidate_id": args.mock_action if args.mock_action.startswith("pair_") else None,
                      "reason": "Explicit mock choice for a local smoke run."}
        else:
            from dotenv import load_dotenv
            load_dotenv(ROOT / ".env", override=False)
            secret = os.environ.get("MUBIT_API_KEY")
            if not secret:
                raise ValueError("Set MUBIT_API_KEY in the environment or .env")
            schema = {"type": "object", "additionalProperties": False,
                      "properties": {"action": {"type": "string", "enum": sorted({c["action"] for c in observation["choices"]})},
                                     "candidate_id": {"type": ["string", "null"],
                                                      "enum": [None] + [c["id"] for c in observation["candidates"]]},
                                     "reason": {"type": "string"}},
                      "required": ["action", "candidate_id", "reason"]}
            payload = {"model": args.model or os.environ.get("MUBIT_MODEL", "gpt-6-luna"),
                       "max_completion_tokens": 1024,
                       "messages": [{"role": "system", "content": (
                           "Choose one advertised nesting action to reduce original-part strip length. "
                           "plain restarts on original parts; pair solves envelopes then refines released original parts when time permits; "
                           "refine warm-starts the current original-part layout and releases all pairs. "
                           "Compare whole_job_lower_bound, area_inflation_fraction, required_utilization_to_beat "
                           "and length_at_baseline_utilization against the current whole-job result. "
                           "The lower bound applies to the grouped seed, not its released refinement, and is not a prediction. "
                           "Large inflation makes a pair costly. "
                           "paired_length_estimate covers only a subset: never compare it to the whole-job length. "
                           "Use only advertised IDs. Set candidate_id to null unless action is pair. "
                           "Return the requested JSON with a brief reason; do not invent coordinates.")},
                           {"role": "user", "content": "Nesting episode: " + uuid.uuid4().hex},
                           {"role": "user", "content": json.dumps(observation, allow_nan=False)}],
                       "response_format": {"type": "json_schema", "json_schema": {
                           "name": "nesting_choice", "strict": True, "schema": schema}}}
            trace.update(endpoint=MUBIT_URL, request=payload)
            save(out / "decision.json", trace)
            request = urllib.request.Request(MUBIT_URL, data=json.dumps(payload).encode(),
                                             headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json"})
            trace["timeout_seconds"] = min(args.model_timeout, remaining(args, reserve=1.5))
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=trace["timeout_seconds"]) as response:
                trace["request_id"] = response.headers.get("x-middleware-request-id")
                trace["response"] = json.load(response)
            if not trace["request_id"]:
                raise ValueError("Mubit response omitted x-middleware-request-id")
            action = json.loads(trace["response"]["choices"][0]["message"]["content"])
        if not isinstance(action, dict) or set(action) != {"action", "candidate_id", "reason"}:
            raise ValueError("Decision must contain action, candidate_id and reason")
        if not isinstance(action["reason"], str) or not action["reason"].strip():
            raise ValueError("Decision reason must be a nonempty string")
        if {"action": action["action"], "candidate_id": action["candidate_id"]} not in observation["choices"]:
            raise ValueError("Decision is not an advertised action/candidate combination")
        remaining(args)
        trace.update(status="completed", action=action)
        return action, trace
    except DeadlineExceeded as error:
        trace.update(status="budget_exhausted", error=str(error))
        raise
    except Exception as error:
        if isinstance(error, urllib.error.HTTPError):
            trace["request_id"] = error.headers.get("x-middleware-request-id")
        trace.update(status="failed", error=str(error))
        raise ValueError(f"Mubit decision failed; see {out / 'decision.json'}: {error}") from None
    finally:
        trace["elapsed_seconds"] = round(time.monotonic() - started, 3)
        save(out / "decision.json", trace)


def write_demo(job, baseline, incumbent, out, record, args):
    remaining(args)
    result, metrics, source, _ = incumbent
    before = json.loads(Path(baseline["solution"]).read_text())
    choice = record["action"]
    choice_label = f"pair({choice['candidate_id']})" if choice["candidate_id"] else choice["action"]
    chooser = "Mock" if record["mode"] == "mock" else "Mubit"
    selected = str(source.relative_to(out))
    action_path = Path(record["action_result"]["solution"]).relative_to(out)
    action_link = action_path.with_name("layout.png") if record["action_result"].get("image") else action_path
    final_link = "layout.png" if (source / "layout.png").is_file() else "solution.json"
    reduction = 1 - metrics["strip_length"] / baseline["strip_length"]
    caption = (f"{chooser} choice: {choice_label}. {choice['reason']} "
               f"Retained: {selected}. Length reduction: {reduction:.2%}. Request: {record['request_id'] or 'mock'}.")
    draw_comparison(job, before, result, out / "comparison.png", caption)
    provenance_path = args.input.with_suffix(".source.json")
    provenance = json.loads(provenance_path.read_text()) if provenance_path.is_file() else {}
    concave = [i["id"] for i in job["items"] if polygon(i["shape"]).convex_hull.area > polygon(i["shape"]).area * 1.000001]
    lines = [f"# {job['name']} nesting demo", "", f"{metrics['part_count']} original parts, {len(job['items'])} types; non-convex types: {concave}.",
             f"Total budget: {args.budget_seconds:g}s; search cap per solve: {args.seconds}s; workers: {args.workers}; seed: {args.seed}.", "",
             "| Layout | Executed length | Original-area utilization |", "| --- | ---: | ---: |"]
    rows = [("Stock baseline", baseline)]
    if "grouped_result" in record:
        rows.append(("Grouped seed", record["grouped_result"]))
        if "refinement_result" in record:
            rows.append(("Released-part refinement", record["refinement_result"]))
    else:
        rows.append(("Chosen action", record["action_result"]))
    for label, data in [*rows, ("Retained result", metrics)]:
        lines.append(f"| {label} | {data['strip_length']:.6f} | {data['utilization']:.2%} |")
    lines += ["", f"Measured retained length reduction: **{reduction:.2%}**. Selected: **{selected}**.",
              f"{chooser} choice: **{choice_label}**. {choice['reason']}",
              f"Request ID: `{record['request_id'] or 'mock; no model request'}`.", "",
              "Original inventory, legal orientations, bounds, overlap and clearance checks passed.",
              f"Native clearance: {job.get('min_item_separation', 0):g}; physical units are those of the input dataset.", "",
              "![Original-part before/after layouts](comparison.png)", "",
              f"[Executed action]({action_link}) · [Final layout]({final_link}) · [Decision trace](decision.json) · [Episode metrics](episode.json)", ""]
    if (out / "pairs.png").is_file():
        lines += ["## Offered pair candidates", "", "![Pair candidates](pairs.png)", ""]
    if provenance:
        lines += ["## Input source", "", provenance["description"],
                  f"[Pinned benchmark input]({provenance['source_url']}) · [Authors' paper]({provenance['paper_url']})",
                  f"Input SHA256: `{provenance['sha256']}`. {provenance['modifications']}", ""]
    (out / "demo.md").write_text("\n".join(lines))
    remaining(args)
    record.update(report=str(out / "demo.md"), comparison=str(out / "comparison.png"),
                  length_reduction_fraction=reduction, provenance=provenance)


def episode(args):
    started = time.monotonic()
    args.deadline = started + args.budget_seconds
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    record = {"status": "completed", "budget_seconds": args.budget_seconds, "baseline_length": None,
              "selected_result": None, "solution": None, "image": None, "candidate_status": "not_run",
              "mode": "mock" if args.mock_action else "mubit", "decision_count": 0}
    incumbent, exports, phase = None, [], "baseline"

    def promote(result, details, source):
        nonlocal incumbent
        remaining(args)
        checked = (result, details, source, time.monotonic() - started)
        exports.append(checked)
        if incumbent is None or result["solution"]["strip_width"] < incumbent[0]["solution"]["strip_width"]:
            incumbent = checked

    previous_handler = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, max(0.001, args.deadline - time.monotonic()))
    try:
        remaining(args)
        job = load_job(args.input)
        save(out / "input.json", job)
        original = {k: v for k, v in job.items() if k != "solution"}
        best, baseline = solve(job, out / "baseline", args)
        finish(job, best, out / "baseline", baseline, args)
        record["baseline_length"] = baseline["strip_length"]
        promote(best, baseline, out / "baseline")
        phase = "pairs"
        try:
            candidate_set = write_pairs(job, out, args, baseline["strip_length"], preview=False)
        except ValueError as error:
            candidate_set = {"candidates": [], "sampling_note": str(error)}
            save(out / "pairs.json", candidate_set)
        candidates = candidate_set["candidates"]
        observation = {"job": job["name"], "strip_height": job["strip_height"],
                       "clearance": job.get("min_item_separation", 0),
                       "inventory": [{"id": i["id"], "demand": i["demand"], "area": polygon(i["shape"]).area,
                                      "hull_area_ratio": polygon(i["shape"]).convex_hull.area / polygon(i["shape"]).area} for i in job["items"]],
                       "current": {k: baseline[k] for k in ["strip_length", "utilization", "part_count"]},
                       "original_job_area": baseline["original_area"],
                       "paired_area_fraction": candidate_set.get("paired_area_fraction", 0),
                       "pairs_available": candidate_set.get("pairs_available", 0),
                       "pair_item_id": candidate_set.get("item_id"),
                       "leftover_count": candidate_set.get("leftover_count", 0),
                       "remaining_seconds": round(remaining(args), 3), "search_seconds_cap": args.seconds,
                       "candidates": [{k: c[k] for k in ["id", "width", "height", "envelope_fill", "rows_in_strip",
                           "paired_length_estimate", "whole_job_area", "whole_job_lower_bound", "area_inflation_fraction",
                           "required_utilization_to_beat", "length_at_baseline_utilization"]} for c in candidates],
                       "choices": [{"action": "plain", "candidate_id": None}, {"action": "refine", "candidate_id": None}]
                                  + [{"action": "pair", "candidate_id": c["id"]} for c in candidates]}
        save(out / "observation.json", observation)
        phase = "decision"
        record["decision_count"] = 1
        action, trace = choose(observation, out, args)
        remaining(args)
        record.update(action=action, request_id=trace["request_id"], candidate_status="pending")
        action_args = argparse.Namespace(**{**vars(args), "seed": args.seed + 1, "refine": False})
        phase = "action"
        if action["action"] == "pair":
            candidate = next(c for c in candidates if c["id"] == action["candidate_id"])
            seed_args = argparse.Namespace(**{**vars(action_args),
                "seconds": min(args.seconds, max(1, int(remaining(args) - 1.5) // 2))})
            result, action_record = grouped_solve(original, candidate, out / "action", seed_args)
        else:
            result, action_record = solve(best if action["action"] == "refine" else original, out / "action", action_args)
            finish(job, result, out / "action", action_record, action_args)
        record["action_result"] = action_record
        promote(result, action_record, out / "action")
        record["candidate_status"] = "accepted" if incumbent[2].name != "baseline" else "not_shorter"
        if action["action"] == "pair":
            record["grouped_result"] = action_record
            record["refinement_status"] = "skipped"
            if remaining(args) >= 1.75:
                phase = "refinement"
                record["refinement_status"] = "pending"
                refined, refinement = solve(result, out / "refine", action_args)
                finish(job, refined, out / "refine", refinement, action_args)
                record.update(refinement_status="completed", refinement_result=refinement)
                promote(refined, refinement, out / "refine")
                if refinement["strip_length"] < action_record["strip_length"]:
                    record["action_result"] = refinement
                record["candidate_status"] = "accepted" if incumbent[2].name != "baseline" else "not_shorter"
            else:
                record["refinement_skip_reason"] = "Not enough budget for a one-second refinement and export"
        phase = "plots"
        rendered = set()
        for result, details, source, _ in [incumbent, *exports]:
            if source not in rendered:
                render_layout(job, result, source, details, args)
                rendered.add(source)
        if candidates:
            remaining(args)
            draw_pairs(job, candidate_set, out / "pairs.png")
            remaining(args)
        if getattr(args, "command", "episode") == "demo":
            phase = "report"
            write_demo(job, baseline, incumbent, out, record, args)
    except DeadlineExceeded as error:
        record.update(status="budget_exhausted", error=str(error), stopped_during=phase)
        if phase == "refinement":
            record["refinement_status"] = "late"
        if record["candidate_status"] == "pending":
            record["candidate_status"] = "late"
    except Exception as error:
        record.update(status="failed", error=str(error), stopped_during=phase)
        if phase == "refinement":
            record["refinement_status"] = "failed"
        if record["candidate_status"] == "pending":
            record["candidate_status"] = "rejected"
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
    # Copy only an immutable, checked export. Missing presentation never loses a solution.
    if incumbent is not None:
        best, metrics, source, accepted = incumbent
        for name in ["solution.json", "validation.json"]:
            shutil.copyfile(source / name, out / name)
        if (source / "layout.png").is_file():
            shutil.copyfile(source / "layout.png", out / "layout.png")
            record["image"] = str(out / "layout.png")
        record.update({k: metrics[k] for k in ["strip_length", "utilization", "original_area", "part_count", "validation"]})
        record.update(selected_result="baseline" if source.name == "baseline" else "action",
                      selected_phase=str(source.relative_to(out)), accepted_elapsed_seconds=round(accepted, 3),
                      solution=str(out / "solution.json"), improvement=record["baseline_length"] - best["solution"]["strip_width"])
        if source.name != "baseline":
            record["candidate_status"] = "accepted"
        if metrics.get("plot_error"):
            record["plot_error"] = metrics["plot_error"]
    elapsed = time.monotonic() - started
    record.update(elapsed_seconds=round(elapsed, 3), deadline_reached=elapsed >= args.budget_seconds,
                  budget_overrun_seconds=round(max(0, elapsed - args.budget_seconds), 3))
    save(out / "run.json", record)
    save(out / "episode.json", record)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ["run", "pairs", "group", "episode", "demo"]:
        sub = commands.add_parser(name)
        sub.add_argument("input", type=Path, **({"nargs": "?", "default": ROOT / "examples/gardeyn4.json"} if name == "demo" else {}))
        sub.add_argument("--out", type=Path, required=True)
        if name != "run":
            sub.add_argument("--item", type=int, help="Repeated part ID; otherwise prefer a concave type")
            sub.add_argument("--keep", type=int, choices=range(1, 13), default=4)
        if name != "pairs":
            sub.add_argument("--seconds", type=int, default=5 if name == "demo" else 3, help="Search seconds per solve, plus startup/export overhead")
            sub.add_argument("--seed", type=int, default=42)
            sub.add_argument("--workers", type=int, choices=range(1, 5), default=2)
            sub.add_argument("--sparrow", type=Path, default=Path(os.environ.get("SPARROW_BIN") or DEFAULT_SPARROW))
        if name == "group":
            sub.add_argument("--candidate", default="pair_0")
            sub.add_argument("--refine", action="store_true", help="Warm-start Sparrow on the expanded original parts")
        if name in ["episode", "demo"]:
            sub.add_argument("--budget-seconds", type=float, default=30, help="Total episode budget, including model, solver, geometry and plots")
            sub.add_argument("--mock-action", help="plain, refine or pair_N; omit for one live Mubit call")
            sub.add_argument("--model", help="Mubit model; defaults to MUBIT_MODEL or gpt-6-luna")
            sub.add_argument("--model-timeout", type=int, choices=range(1, 61), default=20)
    args = parser.parse_args()
    if args.command != "pairs" and not 1 <= args.seconds <= 3600:
        parser.error("--seconds must be between 1 and 3600")
    if args.command in ["episode", "demo"] and not 0 < args.budget_seconds <= 3600:
        parser.error("--budget-seconds must be greater than zero and at most 3600")
    result = {"run": run, "pairs": pairs, "group": group, "episode": episode, "demo": episode}[args.command](args)
    print(json.dumps(result, indent=2))
    if result.get("status") in ["failed", "budget_exhausted"]:
        sys.exit(2 if result["status"] == "budget_exhausted" else 1)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError) as error:
        sys.exit(f"poc: {error}")
