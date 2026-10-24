"""A bounded reasoning-and-tool loop, exclusively through Mubit's Responses gateway."""
import base64
from copy import deepcopy
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request
import uuid

URL = "https://api.mubit.ai/openai/v1/responses"
ROOT = Path(__file__).resolve().parent
INSTRUCTIONS = """You are compiling geometric packing hypotheses for unchanged Sparrow.
You receive complete original contours, permissions, poses, neighbours, occupancy
and an image. Find reusable relations: complementary concavities, alternating
orientations, mixed-type cavity filling or nested stacks. Do not guess empty
world-space pockets or schedule solver micro-runs. Sparrow handles global placement.
Use inspect_patterns to batch up to three hypotheses. A pattern can reference
observed instance IDs OR give NEW relative poses of original part types; set the
unused list empty. Explicit lower_left anchors position the rotated/reflected
part's bounding-box corner; the compiler handles its native coordinate origin.
Ground novel arrangements in the contour/cavity analysis aids and exact geometry.
Include a useful new relation when supported by geometry, rather than just copying
one pair already discovered by the solver. Internal overlap and legal permissions
are checked mechanically; invalid patterns return errors independently.
The compiler joins members by thin bridges and RETAINS NON-CONVEX CONTOURS.
Accessible cavities remain available to other original parts. It never replaces
members by their convex hull. Closed holes must be filled because native items
cannot contain holes; measured closed_hole_area and whole-job inflation expose
that cost. Outline fill near 100% does NOT mean a compact footprint: also inspect
convex_hull_fill, bbox_fill, span and bridge_length. Geometry fill is not a
prediction of final packing quality.
Then finish with up to three measured pattern IDs and repetition counts. Combined
counts must fit the shared original inventory; remaining copies stay independent.
The runner evaluates the COMPLETE native construction with optimization DISABLED,
expands original poses, and compares its length with the stock initializer. A
geometrically valid pattern can still produce a worse global layout. Rejected
recipes return the actual layout and remain correctable: reduce counts or change
relations, or choose an empty recipe to use stock. Do not defend a rejected
recipe using local fill ratios. Accepted recipes finish analysis; ALL timed search
then runs once on independent ORIGINAL parts. Compounds are never optimized.
The checked incumbent survives unsuccessful formulations or model failure.
Your role is to synthesize useful part relationships and a global reformulation,
not narrate native progress or compare a partial estimate with the full objective.
Give concise hypotheses, evidence and uncertainty. Measurements are not gains;
only executed validated results establish gains. Prefer one batched inspection
and one recipe, correcting a rejected novel pattern only when worthwhile.
"""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def function(name, description, properties):
    return {"type": "function", "name": name, "description": description, "strict": True,
            "parameters": {"type": "object", "properties": properties,
                           "required": list(properties), "additionalProperties": False}}


S = {"type": "string"}
STRINGS = {"type":"array","items":S}
NUMBERS = {"type":"array","items":{"type":"number"}}
TOOLS = [
    function("inspect_patterns", "Measure up to three observed or newly synthesized patterns without native search. Returns contour-preserving compound IDs, permission/count limits and inflation costs.", {
        "layout_id":S, "patterns":{"type":"array","minItems":1,"maxItems":3,"items":{
            "type":"object","properties":{
                "source_instances":STRINGS,
                "members":{"type":"array","items":{"type":"object","properties":{
                    "item_id":{"type":"integer"},"rotation":{"type":"number"},
                    "lower_left":{**NUMBERS,"minItems":2,"maxItems":2},"reflected":{"type":"boolean"}},
                    "required":["item_id","rotation","lower_left","reflected"],"additionalProperties":False}},
                "hypothesis":S,"evidence":STRINGS},
            "required":["source_instances","members","hypothesis","evidence"],"additionalProperties":False}}}),
    function("finish", "Evaluate a complete recipe with zero optimizer search. Only an improved original-part initializer ends analysis; rejected recipes return global feedback. Empty patterns selects stock.", {
        "patterns":{"type":"array","maxItems":3,"items":{"type":"object","properties":{
            "candidate_id":S,"copies":{"type":"integer","minimum":1}},
            "required":["candidate_id","copies"],"additionalProperties":False}},
        "summary":S,"limitations":S}),
]


def write(path, data):
    path.write_text(json.dumps(data, indent=2, allow_nan=False) + "\n")


def brief(value):
    if isinstance(value, dict):
        return {key: brief(item) for key, item in value.items() if key not in ["snapshot", "envelope"]}
    if isinstance(value, list):
        return [brief(item) for item in value]
    return value


def compact(journal):
    return {**journal, "turns": [brief({k: v for k, v in turn.items() if k not in ["request", "response"]}) for turn in journal["turns"]]}


def analyse(context, observe, execute, folder, args, remaining, image_path=None):
    folder.mkdir(parents=True, exist_ok=False)
    write(folder / "context.json", context)
    journal = {"status": "running", "model": "mock" if args.mock_plan else args.model, "reasoning_effort": args.reasoning_effort,
               "context_characters": len(json.dumps(context)), "turns": []}
    history = [{"role": "user", "content": "Nesting investigation: " + uuid.uuid4().hex}]
    content = [{"type": "input_text", "text": json.dumps(context, separators=(",", ":"), allow_nan=False)}]
    if image_path is not None and image_path.is_file():
        content.append({"type": "input_image", "image_url": "data:image/png;base64," + base64.b64encode(image_path.read_bytes()).decode(), "detail": "high"})
    history.append({"role": "user", "content": content})
    mock = json.loads(args.mock_plan.read_text()) if args.mock_plan else None
    secret = None
    if mock is None:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env", override=False)
        secret = os.environ.get("MUBIT_API_KEY")
        if not secret:
            raise ValueError("Set MUBIT_API_KEY in the environment or .env")
        journal["model"] = args.model or os.environ.get("MUBIT_MODEL", "gpt-6.1-sol")
    try:
        for index in range(args.planner_turns):
            if mock is not None and index >= len(mock):
                journal["status"] = "completed"
                break
            if remaining() < 4:
                journal["status"] = "budget_guard"
                break
            state = {**observe(), "planner_turn": index+1, "model_turns_remaining": args.planner_turns-index}
            history.append({"role": "user", "content": json.dumps(state, separators=(",", ":"), allow_nan=False)})
            trace = {"turn": index, "mode": "mock" if mock is not None else "mubit", "request_id": None, "status": "pending", "calls": []}
            journal["turns"].append(trace)
            path = folder / f"turn-{index}.json"
            started = time.monotonic()
            if mock is not None:
                calls = [{"type": "function_call", "call_id": f"mock_{index}", "name": mock[index]["name"],
                          "arguments": json.dumps(mock[index]["arguments"])}]
                output = calls
            else:
                payload = {"model": journal["model"], "instructions": INSTRUCTIONS,
                           "input": history, "tools": TOOLS, "parallel_tool_calls": False,
                           "reasoning": {"effort": args.reasoning_effort}, "store": False,
                           "max_output_tokens": args.max_output_tokens}
                trace.update(endpoint=URL, request=deepcopy(payload))
                write(path, trace)
                request = urllib.request.Request(URL, data=json.dumps(payload, allow_nan=False).encode(),
                    headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json"})
                try:
                    timeout = min(args.model_timeout, remaining()-3)
                    if timeout <= 0:
                        raise TimeoutError("Insufficient shared budget for a model response")
                    with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as response:
                        trace["request_id"] = response.headers.get("x-middleware-request-id")
                        body = json.load(response)
                    trace.update(response=body, usage=body.get("usage", {}))
                    if not trace["request_id"]:
                        raise ValueError("Mubit response omitted x-middleware-request-id")
                    if body.get("status") != "completed":
                        raise ValueError(f"Model response {body.get('status')}: {body.get('incomplete_details') or body.get('error')}")
                    output = body.get("output", [])
                    calls = [item for item in output if item["type"] == "function_call"]
                except Exception as error:
                    if isinstance(error, urllib.error.HTTPError):
                        trace["request_id"] = error.headers.get("x-middleware-request-id")
                        trace["provider_error"] = error.read().decode(errors="replace")[:3000].replace(secret, "[redacted]")
                    trace.update(status="failed", error=str(error), model_seconds=round(time.monotonic()-started, 3))
                    write(path, trace)
                    raise
            trace["model_seconds"] = round(time.monotonic()-started, 3)
            # Preserve every output item, including opaque reasoning, for stateless continuation.
            history.extend(output)
            trace["analysis"] = [part["text"] for item in output if item["type"] == "message"
                                 for part in item.get("content", []) if part["type"] == "output_text"]
            done = not calls
            for call in calls:
                remaining()
                arguments = json.loads(call["arguments"])
                entry = {"name": call["name"], "arguments": arguments, "status": "running"}
                trace["calls"].append(entry)
                write(path, trace)
                try:
                    result = execute(call["name"], arguments)
                except (ValueError, KeyError, TypeError) as error:
                    result = {"error": str(error), "incumbent_preserved": True}
                entry.update(status="completed", result=result)
                history.append({"type": "function_call_output", "call_id": call["call_id"],
                                "output": json.dumps(result, allow_nan=False)})
                done = done or (call["name"] == "finish" and "error" not in result)
            trace["status"] = "completed"
            write(path, trace)
            write(folder / "planner.json", compact(journal))
            if done:
                journal["status"] = "completed"
                break
        else:
            journal["status"] = "turn_limit"
        return compact(journal)
    except Exception as error:
        journal.update(status="budget_exhausted" if isinstance(error, TimeoutError) else "failed", error=str(error))
        if journal["turns"]:
            trace.update(status=journal["status"], error=str(error))
            write(path, trace)
        raise
    finally:
        write(folder / "planner.json", compact(journal))
        lines = ["# Model investigation", "", f"Model: {journal['model']}; reasoning: {args.reasoning_effort}; complete context: {journal['context_characters']} characters.", ""]
        for turn in journal["turns"]:
            lines += [f"## Turn {turn['turn'] + 1}", "", f"Mubit request: `{turn['request_id'] or 'mock'}`; model time: {turn.get('model_seconds', 0):g}s.", ""]
            lines += turn.get("analysis", [])
            for call in turn["calls"]:
                lines += [f"### {call['name']}", "", "```json", json.dumps(brief(call), indent=2), "```", ""]
        (folder / "analysis.md").write_text("\n".join(lines))
