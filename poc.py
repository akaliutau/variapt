#!/usr/bin/env python3
"""Compact Sparrow PoC: native solves and a Mubit geometric-analysis loop."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time

from geometry import (check_layout, draw_comparison, draw_layout, draw_pairs, expand_pairs,
                      group_job, layout_metrics, layout_snapshot, original_solution,
                      pair_candidates, polygon, inspect_pattern, placed)
from planner import analyse

ROOT = Path(__file__).resolve().parent
DEFAULT_SPARROW = ROOT / ".cache/sparrow-target/release/sparrow"


class DeadlineExceeded(TimeoutError):
    pass


def remaining(args, reserve=0):
    left = getattr(args, "deadline", float("inf")) - time.monotonic() - reserve
    if left <= 0:
        raise DeadlineExceeded("Episode budget exhausted")
    return left


def expire(signum, frame):
    raise DeadlineExceeded("Episode deadline reached")


def search_remaining(args):
    budget = getattr(args, "search_budget", None)
    return budget["limit_seconds"] - budget["allocated_seconds"] if budget is not None else args.seconds


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


def job_signature(job):
    original = {key:value for key,value in job.items() if key != "solution"}
    return hashlib.sha256(json.dumps(original,sort_keys=True,separators=(",",":")).encode()).hexdigest()


def recipe_payload(job, patterns, selection, provenance):
    items = {item["id"]:item for item in job["items"]}
    proposals = []
    for entry in selection:
        pattern = patterns[entry["candidate_id"]]
        members = []
        for member in pattern["members"]:
            pose = member["transformation"]
            bounds = placed(polygon(items[member["item_id"]]["shape"]),pose).bounds
            members.append({"item_id":member["item_id"],"rotation":pose["rotation"],
                "lower_left":list(bounds[:2]),"reflected":pose.get("reflected",False)})
        proposals.append({"copies":entry["copies"],"specification":{"source_instances":[],
            "members":members,"hypothesis":pattern["hypothesis"],"evidence":pattern["evidence"]}})
    return {"version":1,"source_job_sha256":job_signature(job),"provenance":provenance,"patterns":proposals}


def solve(job, out, args):
    available = remaining(args, reserve=0.5)
    construct = getattr(args,"construct_only",False)
    seconds = 0 if construct else min(args.seconds,int(available-0.25)) if hasattr(args,"deadline") else args.seconds
    if getattr(args, "search_budget", None) is not None and not construct:
        seconds = min(seconds, search_remaining(args))
        if search_remaining(args) < 1:
            raise ValueError("The episode's native search allowance is exhausted")
    if seconds < 1 and not construct:
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
              "search_seconds": seconds, "construction_only":construct, "command": command}
    try:
        with (out / "solver.log").open("w") as log:
            timeout = remaining(args, reserve=0.5) if hasattr(args, "deadline") else seconds + 5
            if getattr(args, "search_budget", None) is not None:
                args.search_budget["allocated_seconds"] += seconds
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


def write_demo(job, baseline, incumbent, out, record, args):
    remaining(args)
    result, metrics, source, _ = incumbent
    before = json.loads(Path(baseline["solution"]).read_text())
    reduction = 1 - metrics["strip_length"] / baseline["strip_length"]
    planner = record.get("planner", {})
    calls = sum(turn.get("mode") == "mubit" for turn in planner.get("turns", []))
    caption = f"Model: {planner.get('model', 'compiled recipe')}; {calls} current Mubit calls. Native selection: {record.get('native_selection', 'unknown')}. Measured initializer gain: {record.get('construction_improvement', 0):.3f}. Final native length: {metrics['strip_length']:.3f}."
    draw_comparison(job, before, result, out / "comparison.png", caption)
    lines = [f"# {job['name']} geometric investigation", "",
             f"{metrics['part_count']} original parts; {args.budget_seconds:g}s wall budget; {args.workers} solver workers.",
             f"Native search allowance: {args.seconds}s total; allocated: {args.search_budget['allocated_seconds']}s including the initial probe.",
             f"Model: {planner.get('model')}; reasoning: {args.reasoning_effort}; full context: {planner.get('context_characters', 0)} characters.", "",
             "| Executed layout | Search seconds | Length | Original-area utilization |", "| --- | ---: | ---: | ---: |",
             f"| Initial stock probe | {baseline['search_seconds']} | {baseline['strip_length']:.6f} | {baseline['utilization']:.2%} |"]
    for trial in record["trials"]:
        lines.append(f"| {trial['layout_id']}: {trial['operation']} | {trial['search_seconds']} | {trial['strip_length']:.6f} | {trial['utilization']:.2%} |")
    lines += [f"| Retained best | — | {metrics['strip_length']:.6f} | {metrics['utilization']:.2%} |", "",
              f"Accepted recipe's initializer improvement: **{record.get('construction_improvement', 0):.6f}** length units.",
              f"Native selection: **{record.get('native_selection', 'unknown')}**. All accepted layouts passed original-part checks.",
              f"Total change from construction to final layout: {reduction:.2%}, including ordinary native optimization. This is not a measurement of model advantage.", "",
              "[Model hypotheses, tool measurements and conclusions](planner/analysis.md) · [Full context](planner/context.json) · [Episode](episode.json)", "",
              "![Original-part comparison](comparison.png)", ""]
    if record.get("analysis_summary"):
        lines += ["Model recipe before native execution:", record["analysis_summary"], record.get("analysis_limitations", ""), ""]
    if record.get("native_fallback"):
        lines += [f"Model investigation failed; native fallback used the checked seed: {record['planner_error']}", ""]
    if record["mode"] == "recipe":
        lines += ["This run loaded a compiled recipe with no new LLM calls. It was checked against the stock initializer; an unhelpful recipe falls back to stock. Original model cost and IDs are in episode.json recipe_provenance.", ""]
        lines = [line for line in lines if "planner/analysis.md" not in line]
    if (out/"recipe.json").is_file():
        lines += ["[Geometrically checked reusable recipe](recipe.json)", ""]
    (out / "demo.md").write_text("\n".join(lines))
    remaining(args)
    record.update(report=str(out / "demo.md"), comparison=str(out / "comparison.png"), length_reduction_fraction=reduction)


def episode(args):
    started = time.monotonic()
    args.deadline = started + args.budget_seconds
    args.search_budget = {"limit_seconds": args.seconds, "allocated_seconds": 0}
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    record = {"status": "completed", "budget_seconds": args.budget_seconds, "baseline_length": None,
              "solution": None, "image": None,
              "mode": "recipe" if getattr(args,"recipe",None) else "mock" if args.mock_plan else "mubit",
              "trials": [], "pattern_measurements": []}
    incumbent, search_seed, layouts, snapshots, patterns = None, None, {}, {}, {}
    mappings = []
    attempt = 0
    phase = "baseline"

    def promote(result, details, source, layout_id):
        nonlocal incumbent
        remaining(args)
        checked = (result, details, source, time.monotonic()-started)
        layouts[layout_id] = checked
        if incumbent is None or result["solution"]["strip_width"] < incumbent[0]["solution"]["strip_width"]:
            incumbent = checked
        return checked

    def selected(layout_id):
        if layout_id == "best":
            return incumbent
        if layout_id not in layouts:
            raise ValueError(f"Unknown layout {layout_id}; available: best, {', '.join(layouts)}")
        return layouts[layout_id]

    def snapshot(layout_id):
        checked = selected(layout_id)
        key = str(checked[2])
        if key not in snapshots:
            snapshots[key] = layout_snapshot(job, checked[0])
        return snapshots[key]

    def publish(result, details, target, operation, hypothesis):
        finish(job, result, target, details, args)
        previous = incumbent[0]["solution"]["strip_width"]
        checked = promote(result, details, target, target.name)
        entry = {"layout_id": target.name, "operation": operation, "hypothesis": hypothesis,
                 **{k: details[k] for k in ["strip_length", "utilization", "search_seconds", "elapsed_seconds"]},
                 "accepted_as_best": incumbent is checked, "solution": details["solution"]}
        record["trials"].append(entry)
        feedback = {**entry, "improvement_over_previous_best": max(0, previous-details["strip_length"])}
        return feedback

    def observe():
        nonlocal phase
        phase = "planner"
        return {"remaining_seconds": remaining(args),
                "analysis_remaining_seconds": max(0, remaining(args)-search_remaining(args)-5),
                "search_budget": {**args.search_budget, "remaining_seconds": search_remaining(args)},
                "available_layouts": [{"id": key, **layout_metrics(job, value[0])} for key, value in layouts.items()],
                "best_layout_id": next(key for key, value in layouts.items() if value is incumbent),
                "completed_trials": record["trials"],
                "measured_patterns": [{k:p[k] for k in ["id","max_copies","envelope_fill","area_inflation_at_max_copies","closed_hole_area"]} for p in patterns.values()]}

    def execute(name, arguments):
        nonlocal phase, mappings, attempt, search_seed
        phase = name
        if name == "inspect_patterns":
            specifications = arguments["patterns"]
            if not 1 <= len(specifications) <= 3:
                raise ValueError("Inspect one to three patterns per batch")
            source = selected(arguments["layout_id"])[0]
            measurements = []
            for specification in specifications:
                try:
                    key = f"pattern_{len(patterns)}"
                    candidate = inspect_pattern(job, source, specification, key)
                    candidate.update(hypothesis=specification["hypothesis"],evidence=specification["evidence"])
                    patterns[key] = candidate
                    measurements.append(candidate)
                except (ValueError, KeyError, TypeError) as error:
                    measurements.append({"hypothesis":specification.get("hypothesis"),"error":str(error)})
            save(out / "patterns.json", patterns)
            record["pattern_measurements"].extend([{k:v for k,v in p.items() if k not in ["envelope","members"]} for p in measurements])
            return {"measured":measurements,"native_search_seconds":0}
        if name == "finish":
            recipe = arguments["patterns"]
            if len(recipe) > 3 or len({p["candidate_id"] for p in recipe}) != len(recipe):
                raise ValueError("Select at most three distinct measured patterns")
            formulation, pending = original, []
            for entry in recipe:
                if entry["candidate_id"] not in patterns:
                    raise ValueError("Use measured pattern IDs")
                formulation, mapping = group_job(formulation,patterns[entry["candidate_id"]],entry["copies"])
                pending.append(mapping)
            if pending:
                attempt += 1
                target = out / "trials" / f"trial_{attempt}"
                seed_args = argparse.Namespace(**{**vars(args),"seed":args.seed,"construct_only":True})
                result, details = solve(formulation,target,seed_args)
                save(target / "mappings.json",pending)
                for mapping in pending:
                    result = expand_pairs(job,result,mapping)
                feedback = publish(result,details,target,"pattern_construction","Evaluate the full original-part initializer with zero optimizer search.")
                gain = baseline["strip_length"]-details["strip_length"]
                record.setdefault("recipe_trials",[]).append({"recipe":recipe,"construction_improvement":gain,"layout_id":target.name})
                if gain <= max(1e-3,baseline["strip_length"]*1e-5):
                    return {"error":"Recipe did not improve the complete stock initializer. Change its relations/counts or finish with an empty recipe.",
                        "baseline_length":baseline["strip_length"],"construction_improvement":gain,**feedback,
                        "snapshot":snapshot(target.name),"native_search_spent":0}
                search_seed = layouts[target.name]
                record["construction_improvement"] = gain
            mappings = pending
            record.update(pattern_recipe=recipe,analysis_summary=arguments["summary"],analysis_limitations=arguments["limitations"])
            return {"finished":True,"groups":mappings,"native_search_reserved":search_remaining(args)}
        raise ValueError(f"Unknown tool: {name}")

    previous_handler = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, max(.001, args.deadline-time.monotonic()))
    try:
        job = load_job(args.input)
        original = {k: v for k, v in job.items() if k != "solution"}
        save(out / "input.json", job)
        if "solution" in job:
            (out / "baseline").mkdir()
            save(out / "baseline/input.json", job)
            best = original_solution(job, job)
            baseline = {"status": "provided", "search_seconds": 0, "elapsed_seconds": 0}
        else:
            initial = argparse.Namespace(**{**vars(args), "seconds": args.initial_seconds,
                "construct_only":args.initial_seconds==0})
            best, baseline = solve(job, out / "baseline", initial)
        finish(job, best, out / "baseline", baseline, args)
        record["baseline_length"] = baseline["strip_length"]
        search_seed = promote(best, baseline, out / "baseline", "baseline")
        phase = "context"
        render_layout(job, best, out / "baseline", baseline, args)
        types = []
        for item in original["items"]:
            shape = polygon(item["shape"])
            void = shape.convex_hull.difference(shape)
            pieces = list(void.geoms) if hasattr(void, "geoms") else [void]
            types.append({"id": item["id"], "area": shape.area, "perimeter": shape.length,
                          "bounds": list(shape.bounds), "hull_area": shape.convex_hull.area,
                          "analysis_outline": {"approximate": True,
                              "vertices": list(shape.simplify(max(shape.bounds[2]-shape.bounds[0],shape.bounds[3]-shape.bounds[1])*.005,preserve_topology=True).exterior.coords)},
                          "hull_voids": [{"area": p.area, "bounds": list(p.bounds),
                              "analysis_vertices": list(p.simplify(max(1,shape.length)*.0005,preserve_topology=True).exterior.coords) if p.geom_type=="Polygon" else []}
                              for p in sorted(pieces, key=lambda p: p.area, reverse=True)[:3] if not p.is_empty]})
        context = {"task": "Reduce the length used by all original parts. Investigate geometric structure with exact tools.",
                   "full_original_job": original, "type_measurements": types, "initial_layout": snapshot("baseline"),
                   "initial_solver_log": (out/"baseline/solver.log").read_text() if (out/"baseline/solver.log").is_file() else "Checked input solution; native search reserved for the final refinement.",
                   "constraints": "All pose changes must be executed and checked on original geometry and inventory.",
                   "budget_seconds": args.budget_seconds, "model_turn_limit": args.planner_turns,
                   "search_budget_seconds": args.seconds,
                   "final_native_search_seconds": search_remaining(args), "workers": args.workers}
        phase = "planner"
        if getattr(args,"recipe",None):
            artifact = json.loads(args.recipe.read_text())
            if artifact.get("version") != 1 or artifact.get("source_job_sha256") != job_signature(job):
                raise ValueError("Recipe belongs to a different original job; recompile it for this input")
            if artifact.get("evaluation",{}).get("status") == "rejected":
                record["recipe_rejected"] = "Prior end-to-end comparison favoured stock; compiled candidate bypassed."
                record["recipe_evaluation"] = artifact["evaluation"]
            else:
                proposals = artifact["patterns"]
                execute("inspect_patterns",{"layout_id":"baseline","patterns":[p["specification"] for p in proposals]})
                if len(patterns) != len(proposals):
                    raise ValueError("Stored recipe failed geometric revalidation; see patterns.json")
                checked_recipe = execute("finish",{"patterns":[{"candidate_id":f"pattern_{i}","copies":p["copies"]} for i,p in enumerate(proposals)],
                    "summary":"Replay the geometrically revalidated compiled recipe; no new model calls.",
                    "limitations":"Compilation cost is excluded from this replay and recorded in recipe provenance."})
                if "error" in checked_recipe:
                    record["recipe_rejected"] = checked_recipe["error"]
            record["recipe_provenance"] = artifact["provenance"]
            record["planner"] = {"status":"recipe_replay","model":artifact["provenance"].get("model"),
                "context_characters":0,"turns":[]}
        else:
            try:
                record["planner"] = analyse(context, observe, execute, out / "planner", args,
                    lambda: max(0, remaining(args)-search_remaining(args)-5), out/"baseline/layout.png")
            except DeadlineExceeded:
                raise
            except Exception as error:
                record.update(planner_error=str(error), native_fallback=True)
        if mappings:
            provenance = record.get("recipe_provenance",{"mode":record["mode"],"model":record.get("planner",{}).get("model"),
                "model_seconds":sum(t.get("model_seconds",0) for t in record.get("planner",{}).get("turns",[])),
                "mubit_request_ids":[t["request_id"] for t in record.get("planner",{}).get("turns",[]) if t.get("request_id")]})
            save(out / "recipe.json",recipe_payload(job,patterns,record["pattern_recipe"],provenance))
        record["refinement_seed"] = str(search_seed[2].relative_to(out))
        record["model_contribution"] = "initializer_improved" if mappings else "none"
        if search_remaining(args) >= 1:
            phase = "native_refinement"
            attempt += 1
            target = out / "trials" / f"trial_{attempt}"
            polish_args = argparse.Namespace(**{**vars(args),"seed":args.seed})
            native_input = search_seed[0] if mappings or "solution" in job else original
            record["native_selection"] = "compiled_seed" if mappings else "provided_seed" if "solution" in job else "stock"
            result, details = solve(native_input, target, polish_args)
            result = original_solution(job, result)
            publish(result, details, target, "native_refinement", "One continuous original-part solve from the selected checked structural seed.")
        phase = "plots"
        for result, details, source, _ in [incumbent, *layouts.values()]:
            if not (source/"layout.png").is_file():
                render_layout(job, result, source, details, args)
        if args.command == "demo":
            phase = "report"
            write_demo(job, baseline, incumbent, out, record, args)
    except DeadlineExceeded as error:
        record.update(status="budget_exhausted", error=str(error), stopped_during=phase)
    except Exception as error:
        record.update(status="failed", error=str(error), stopped_during=phase)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
    if (out/"planner/planner.json").is_file():
        record["planner"] = json.loads((out/"planner/planner.json").read_text())
    if incumbent is not None:
        best, details, source, accepted = incumbent
        for name in ["solution.json", "validation.json"]:
            shutil.copyfile(source/name, out/name)
        if (source/"layout.png").is_file():
            shutil.copyfile(source/"layout.png", out/"layout.png")
            record["image"] = str(out/"layout.png")
        record.update({key: details[key] for key in ["strip_length", "utilization", "original_area", "part_count"]},
                      validation=details["validation"], solution=str(out/"solution.json"),
                      selected_phase=str(source.relative_to(out)), accepted_elapsed_seconds=round(accepted, 3),
                      improvement=record["baseline_length"]-best["solution"]["strip_width"])
    elapsed = time.monotonic()-started
    record.update(elapsed_seconds=round(elapsed, 3), budget_overrun_seconds=round(max(0, elapsed-args.budget_seconds), 3),
                  search_budget={**args.search_budget, "remaining_seconds": search_remaining(args)})
    save(out/"episode.json", record)
    save(out/"run.json", record)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ["run", "pairs", "group", "episode", "demo"]:
        sub = commands.add_parser(name)
        sub.add_argument("input", type=Path, **({"nargs": "?", "default": ROOT / "examples/gardeyn4.json"} if name == "demo" else {}))
        sub.add_argument("--out", type=Path, required=True)
        if name in ["pairs", "group"]:
            sub.add_argument("--item", type=int, help="Repeated part ID; otherwise prefer a concave type")
            sub.add_argument("--keep", type=int, choices=range(1, 13), default=4)
        if name != "pairs":
            analyst = name in ["episode", "demo"]
            sub.add_argument("--seconds", type=int, default=30 if analyst else 3,
                             help="Total native search seconds across the initial probe and all trials" if analyst else "Search seconds per solve, plus startup/export overhead")
            sub.add_argument("--seed", type=int, default=42)
            sub.add_argument("--workers", type=int, choices=range(1, 5), default=2)
            sub.add_argument("--sparrow", type=Path, default=Path(os.environ.get("SPARROW_BIN") or DEFAULT_SPARROW))
        if name == "group":
            sub.add_argument("--candidate", default="pair_0")
            sub.add_argument("--refine", action="store_true", help="Warm-start Sparrow on the expanded original parts")
        if name in ["episode", "demo"]:
            sub.add_argument("--initial-seconds", type=int, default=0, help="Initial search time; zero constructs a baseline without optimization")
            sub.add_argument("--budget-seconds", type=float, default=120, help="Shared model, solver, geometry and plotting budget")
            sub.add_argument("--mock-plan", type=Path, help="Explicit local tool sequence; no model calls or credentials")
            sub.add_argument("--recipe",type=Path,help="Replay a compiled recipe without model calls; all geometry is revalidated")
            sub.add_argument("--model", help="Mubit model; defaults to MUBIT_MODEL or gpt-6.1-sol")
            sub.add_argument("--reasoning-effort", choices=["low", "medium", "high"], default="medium")
            sub.add_argument("--planner-turns", type=int, choices=range(1, 9), default=3)
            sub.add_argument("--max-output-tokens", type=int, default=8192)
            sub.add_argument("--model-timeout", type=int, choices=range(1, 61), default=60)
    args = parser.parse_args()
    if args.command != "pairs" and not 1 <= args.seconds <= 3600:
        parser.error("--seconds must be between 1 and 3600")
    if args.command in ["episode", "demo"] and not 0 < args.budget_seconds <= 3600:
        parser.error("--budget-seconds must be greater than zero and at most 3600")
    if args.command in ["episode", "demo"] and not 0 <= args.initial_seconds <= 3600:
        parser.error("--initial-seconds must be between 0 and 3600")
    if args.command in ["episode","demo"] and args.recipe and args.mock_plan:
        parser.error("Choose --recipe or --mock-plan")
    if args.command in ["episode", "demo"] and not 1024 <= args.max_output_tokens <= 16384:
        parser.error("--max-output-tokens must be between 1024 and 16384")
    result = {"run": run, "pairs": pairs, "group": group, "episode": episode, "demo": episode}[args.command](args)
    print(json.dumps(result, indent=2))
    if result.get("status") in ["failed", "budget_exhausted"]:
        sys.exit(2 if result["status"] == "budget_exhausted" else 1)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError) as error:
        sys.exit(f"poc: {error}")
