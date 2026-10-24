"""Native polygons, measured motifs, pose expansion, validation and pictures."""
import math
from collections import Counter
from copy import deepcopy
from itertools import combinations

from shapely import affinity
from shapely.geometry import LineString, Polygon, box
from shapely.ops import nearest_points, unary_union


def polygon(shape):
    if shape["type"] == "simple_polygon":
        result = Polygon(shape["data"])
    elif shape["type"] == "rectangle":
        d = shape["data"]
        result = box(d["x_min"], d["y_min"], d["x_min"] + d["width"], d["y_min"] + d["height"])
    else:
        raise ValueError("This PoC accepts simple_polygon and rectangle parts")
    if result.is_empty or not result.is_valid or result.area <= 0:
        raise ValueError("Part must be a valid polygon with positive area")
    return result


def placed(shape, pose):
    if pose.get("reflected", False):
        shape = affinity.scale(shape, xfact=1, yfact=-1, origin=(0, 0))
    shape = affinity.rotate(shape, pose["rotation"] % 360, origin=(0, 0))
    return affinity.translate(shape, *pose["translation"])


def sampled_angles(item):
    rotation = item["orientation"]["rotation"]
    if rotation["mode"] == "continuous":
        return [0.0, 90.0, 180.0, 270.0]
    if rotation["mode"] == "stepped":
        step = float(rotation["step"])
        if not math.isfinite(step) or not 0 < step <= 360:
            raise ValueError("Invalid rotation step")
        count = round(360 / step)
        if count > 65536 or abs(count * step - 360) > 0.0001:
            raise ValueError("Rotation step must divide 360 degrees")
        return [360.0 * (i * count // min(count, 4)) / count for i in range(min(count, 4))]
    if rotation["mode"] != "discrete":
        raise ValueError("Unknown rotation mode")
    angles = sorted({float(a) % 360 for a in rotation["angles"]})
    if not angles or not all(math.isfinite(a) for a in angles):
        raise ValueError("A nonempty list of finite legal rotations is required")
    return [angles[i * len(angles) // min(4, len(angles))] for i in range(min(4, len(angles)))]


def legal_angle(item, angle):
    rotation = item["orientation"]["rotation"]
    angle %= 360
    if rotation["mode"] == "continuous":
        return True
    if rotation["mode"] == "stepped":
        step = rotation["step"]
        return abs(angle - round(angle / step) * step) <= 1e-4
    return any(abs((angle - a + 180) % 360 - 180) <= 1e-4 for a in rotation["angles"])


def pair_candidates(job, item_id=None, keep=4, incumbent_length=None):
    """Sample vertex contacts of buffered copies; this is deliberately not an NFP engine."""
    eligible = [i for i in job["items"] if i["demand"] >= 2 and (item_id is None or i["id"] == item_id)]
    if not eligible:
        raise ValueError("Select a part type with demand >= 2")
    item = max(eligible, key=lambda i: (polygon(i["shape"]).convex_hull.area / polygon(i["shape"]).area, i["demand"]))
    original = polygon(item["shape"])
    clearance = float(job.get("min_item_separation", 0))
    height = float(job["strip_height"])
    if not math.isfinite(clearance) or clearance < 0 or not math.isfinite(height) or height <= 2 * clearance:
        raise ValueError("Invalid strip height or clearance")
    total_area = sum(polygon(i["shape"]).area * i["demand"] for i in job["items"])
    pair_count = item["demand"] // 2
    angles = sampled_angles(item)
    eps = max(1, *(abs(v) for v in original.bounds)) * 1e-6
    shapes = {a: affinity.rotate(original, a, origin=(0, 0)) for a in angles}
    contacts = {}
    for angle, shape in shapes.items():
        vertices = list(shape.buffer(clearance / 2 + eps, join_style=2).exterior.coords)[:-1]
        contacts[angle] = [vertices[i * len(vertices) // min(12, len(vertices))] for i in range(min(12, len(vertices)))]
    best = {}
    tried = pruned = 0
    for a_angle, a in shapes.items():
        for b_angle, b in shapes.items():
            turns = sorted({(r - a_angle) % 360 for r in angles if legal_angle(item, b_angle + r - a_angle)})
            orientation = {"mode": "continuous"} if item["orientation"]["rotation"]["mode"] == "continuous" else {"mode": "discrete", "angles": turns}
            offsets = sorted({(x - u, y - v) for x, y in contacts[a_angle] for u, v in contacts[b_angle]})
            for dx, dy in offsets:
                tried += 1
                moved = affinity.translate(b, dx, dy)
                if a.intersection(moved).area > original.area * 1e-10 or a.distance(moved) + eps / 100 < clearance:
                    continue
                hull = a.union(moved).convex_hull
                x0, y0, x1, y1 = hull.bounds
                width, pair_height = x1 - x0, y1 - y0
                rows = math.floor((height - clearance + eps / 100) / (pair_height + clearance))
                if rows < 1:
                    continue
                columns = (pair_count + rows - 1) // rows
                estimate = columns * width + (columns + 1) * clearance
                fill = 2 * original.area / hull.area
                grouped_area = total_area + pair_count * (hull.area - 2 * original.area)
                lower_bound = 2 * clearance + grouped_area / (height - 2 * clearance)
                if incumbent_length is not None and lower_bound >= incumbent_length:
                    pruned += 1
                    continue
                envelope = affinity.translate(hull, -x0, -y0)
                candidate = {
                    "members": [
                        {"item_id": item["id"], "transformation": {"rotation": a_angle, "translation": [-x0, -y0]}},
                        {"item_id": item["id"], "transformation": {"rotation": b_angle, "translation": [dx - x0, dy - y0]}},
                    ],
                    "envelope": {"type": "simple_polygon", "data": list(envelope.exterior.coords)[:-1]},
                    "orientation": {"rotation": orientation}, "width": width, "height": pair_height,
                    "envelope_fill": fill, "rows_in_strip": rows, "paired_length_estimate": estimate,
                    "whole_job_area": grouped_area, "whole_job_lower_bound": lower_bound,
                    "area_inflation_fraction": grouped_area / total_area - 1,
                    "required_utilization_to_beat": grouped_area / (height * incumbent_length) if incumbent_length else None,
                    "length_at_baseline_utilization": incumbent_length * grouped_area / total_area if incumbent_length else None,
                    "member_separation": a.distance(moved),
                }
                # Preserve geometry, local poses and permissions; bounding boxes do not establish equivalence.
                poses = tuple(sorted((m["item_id"], m["transformation"]["rotation"], *m["transformation"]["translation"])
                                     for m in candidate["members"]))
                key = (envelope.normalize().wkb, poses, orientation["mode"], tuple(orientation.get("angles", [])))
                best.setdefault(key, candidate)
    candidates = sorted(best.values(), key=lambda c: (c["whole_job_lower_bound"], c["paired_length_estimate"], -c["envelope_fill"]))[:keep]
    if not candidates:
        raise ValueError("No sampled pair fits with an admissible whole-job grouped-area bound; try another part")
    for index, candidate in enumerate(candidates):
        candidate["id"] = f"pair_{index}"
    return {"job": job["name"], "item_id": item["id"], "pairs_available": item["demand"] // 2,
            "leftover_count": item["demand"] % 2, "clearance": clearance,
            "original_job_area": total_area, "paired_area_fraction": 2 * pair_count * original.area / total_area,
            "sampled_angles": angles, "contacts_tried": tried, "pruned_by_area_bound": pruned, "candidates": candidates}


def group_job(job, candidate, copies=None):
    """Repeat a rigid motif, including different part types, without changing inventory."""
    grouped = deepcopy({k: v for k, v in job.items() if k != "solution"})
    needed = Counter(m["item_id"] for m in candidate["members"])
    demands = {i["id"]: i["demand"] for i in job["items"]}
    maximum = min(demands[key] // quantity for key, quantity in needed.items())
    count = maximum if copies is None else copies
    if type(count) is not int or not 1 <= count <= maximum:
        raise ValueError(f"Motif copies must be between 1 and {maximum}")
    group_id = max(i["id"] for i in grouped["items"]) + 1
    for item in grouped["items"]:
        item["demand"] -= count * needed[item["id"]]
    grouped["items"] = [i for i in grouped["items"] if i["demand"]]
    grouped["items"].append({"id": group_id, "demand": count,
                             "shape": deepcopy(candidate["envelope"]),
                             "orientation": deepcopy(candidate["orientation"])})
    mapping = {"candidate_id": candidate["id"], "group_item_id": group_id,
               "groups": count, "leftover_counts": {key: demands[key] - count * n for key, n in needed.items()},
               "members": candidate["members"]}
    if len(needed) == 1:
        mapping["leftover_count"] = next(iter(mapping["leftover_counts"].values()))
    return grouped, mapping


def expand_pairs(job, result, mapping):
    """Compose the group's world pose with each member's original local pose."""
    expanded = deepcopy({k: v for k, v in job.items() if k != "solution"})
    expanded["solution"] = deepcopy(result["solution"])
    placements = []
    for placement in result["solution"]["layout"]["placed_items"]:
        if placement["item_id"] != mapping["group_item_id"]:
            placements.append(deepcopy(placement))
            continue
        world = placement["transformation"]
        if world.get("reflected", False):
            raise ValueError("Pair envelope reflection is disabled")
        angle = math.radians(world["rotation"])
        c, s = math.cos(angle), math.sin(angle)
        for member in mapping["members"]:
            local = member["transformation"]
            x, y = local["translation"]
            placements.append({"item_id": member["item_id"], "transformation": {
                "rotation": (world["rotation"] + local["rotation"]) % 360,
                "reflected": local.get("reflected", False),
                "translation": [world["translation"][0] + c*x - s*y,
                                world["translation"][1] + s*x + c*y]}})
    solution = expanded["solution"]
    solution["layout"]["placed_items"] = placements
    area = sum(polygon(i["shape"]).area * i["demand"] for i in job["items"])
    density = area / (solution["strip_width"] * job["strip_height"])
    solution["density"] = solution["layout"]["density"] = density
    return expanded


def original_solution(job, result):
    """Restore the original job header and original-part density."""
    restored = deepcopy({k: v for k, v in job.items() if k != "solution"})
    restored["solution"] = deepcopy(result["solution"])
    area = sum(polygon(i["shape"]).area * i["demand"] for i in job["items"])
    density = area / (restored["solution"]["strip_width"] * job["strip_height"])
    restored["solution"]["density"] = restored["solution"]["layout"]["density"] = density
    return restored


def instances(result):
    counts = Counter()
    found = {}
    for placement in result["solution"]["layout"]["placed_items"]:
        key = placement["item_id"]
        counts[key] += 1
        found[f"{key}:{counts[key]}"] = placement
    return found


def legal_pose(item, pose, turn=0):
    axes = item["orientation"].get("reflection_axes", []) if pose.get("reflected", False) else [0]
    return any(legal_angle(item, pose["rotation"] + turn - 2*a) for a in axes)


def inspect_pattern(job, result, specification, candidate_id):
    """Compile an observed or newly synthesized relation into a contour-preserving part."""
    items = {i["id"]: i for i in job["items"]}
    source, proposed = specification["source_instances"], specification["members"]
    if bool(source) == bool(proposed):
        raise ValueError("Use either source_instances or explicit members, with the other empty")
    if source:
        found = instances(result)
        if len(set(source)) != len(source) or any(key not in found for key in source):
            raise ValueError("Use distinct instance IDs from the named layout")
        members = deepcopy([found[key] for key in source])
    else:
        members = []
        for p in proposed:
            lower = p["lower_left"]
            if type(p["item_id"]) is not int or p["item_id"] not in items or len(lower) != 2 or not all(type(v) in [int,float] and math.isfinite(v) for v in lower):
                raise ValueError("Use known original types and finite lower-left anchors")
            if type(p["rotation"]) not in [int,float] or not math.isfinite(p["rotation"]) or type(p["reflected"]) is not bool:
                raise ValueError("Use a finite rotation and a boolean reflection flag")
            pose = {"rotation":p["rotation"],"translation":[0,0],"reflected":p["reflected"]}
            x0,y0,_,_ = placed(polygon(items[p["item_id"]]["shape"]),pose).bounds
            pose["translation"] = [lower[0]-x0,lower[1]-y0]
            members.append({"item_id":p["item_id"],"transformation":pose})
    if not 2 <= len(members) <= 6:
        raise ValueError("A pattern needs two to six original parts")
    needed = Counter(m["item_id"] for m in members)
    if any(key not in items or count > items[key]["demand"] for key,count in needed.items()):
        raise ValueError("Pattern exceeds original inventory")
    for m in members:
        pose = m["transformation"]
        if len(pose["translation"]) != 2 or not all(type(v) in [int,float] and math.isfinite(v) for v in [pose["rotation"],*pose["translation"]]):
            raise ValueError("Pattern poses must contain finite original-coordinate rotations/translations")
        if not legal_pose(items[m["item_id"]], pose):
            raise ValueError("Pattern uses a forbidden orientation or reflection")
    shapes = [placed(polygon(items[m["item_id"]]["shape"]),m["transformation"]) for m in members]
    clearance = job.get("min_item_separation",0)
    for a,b in combinations(shapes,2):
        tolerance = 1e-6 if source else 1e-8
        linear = min(max(a.bounds[2]-a.bounds[0],a.bounds[3]-a.bounds[1]),
                     max(b.bounds[2]-b.bounds[0],b.bounds[3]-b.bounds[1]))*2e-6 if source else 1e-7
        if a.intersection(b).area > min(a.area,b.area)*tolerance or a.distance(b)+linear < clearance:
            raise ValueError("Pattern members overlap or violate clearance")
    union = unary_union(shapes)
    epsilon = max(1, union.bounds[2]-union.bounds[0], union.bounds[3]-union.bounds[1])*2e-5
    connected = union
    bridge_length = 0
    # Sparrow items are simple polygons. Join disconnected members by thin bridges,
    # retaining every accessible cavity instead of replacing the union with its hull.
    while connected.geom_type == "MultiPolygon":
        pieces = list(connected.geoms)
        a,b = min(combinations(pieces,2),key=lambda pair:pair[0].distance(pair[1]))
        bridge_length += a.distance(b)
        connected = unary_union([connected,LineString(nearest_points(a,b)).buffer(epsilon)])
    if connected.geom_type != "Polygon" or not connected.is_valid:
        raise ValueError("Pattern cannot form a valid connected outline")
    outline = Polygon(connected.exterior)
    original_area = sum(shape.area for shape in shapes)
    if union.difference(outline).area > original_area*1e-9:
        raise ValueError("Pattern outline does not conservatively contain its originals")
    x0,y0,x1,y1 = outline.bounds
    for m in members:
        pose = m["transformation"]
        pose["translation"] = [pose["translation"][0]-x0,pose["translation"][1]-y0]
    discrete = next((m for m in members if items[m["item_id"]]["orientation"]["rotation"]["mode"] != "continuous"),None)
    rotation = {"mode":"continuous"}
    if discrete is not None:
        item,pose = items[discrete["item_id"]],discrete["transformation"]
        axes = item["orientation"].get("reflection_axes",[]) if pose.get("reflected",False) else [0]
        turns = {0.0,*((r+2*a-pose["rotation"])%360 for r in sampled_angles(item) for a in axes)}
        rotation = {"mode":"discrete","angles":sorted(t for t in turns if all(legal_pose(items[m["item_id"]],m["transformation"],t) for m in members))}
    maximum = min(items[key]["demand"]//quantity for key,quantity in needed.items())
    whole_area = sum(polygon(i["shape"]).area*i["demand"] for i in job["items"])
    return {"id":candidate_id,"source_instances":source,"members":members,
        "envelope":{"type":"simple_polygon","data":list(affinity.translate(outline,-x0,-y0).exterior.coords)[:-1]},
        "orientation":{"rotation":rotation},"width":x1-x0,"height":y1-y0,
        "envelope_fill":original_area/outline.area,"max_copies":maximum,
        "area_inflation_at_max_copies":maximum*(outline.area-original_area)/whole_area,
        "convex_hull_area":union.convex_hull.area,"outline_area":outline.area,"original_area":original_area,
        "convex_hull_fill":original_area/union.convex_hull.area,
        "bbox_fill":original_area/((x1-x0)*(y1-y0)),"bridge_length":bridge_length,
        "closed_hole_area":outline.area-connected.area,
        "note":"Accessible concavities are retained. Closed holes are filled because native items cannot contain holes."}


def layout_snapshot(job, result):
    """Exact poses plus measured neighbourhoods and strip occupancy for model analysis."""
    items = {i["id"]: i for i in job["items"]}
    found = instances(result)
    shapes = {key: placed(polygon(items[p["item_id"]]["shape"]), p["transformation"]) for key, p in found.items()}
    neighbours = {key: [] for key in found}
    for a, b in combinations(found, 2):
        distance = shapes[a].distance(shapes[b])
        ax0, ay0, ax1, ay1 = shapes[a].bounds
        bx0, by0, bx1, by1 = shapes[b].bounds
        overlap = max(0, min(ax1,bx1)-max(ax0,bx0)) * max(0, min(ay1,by1)-max(ay0,by0))
        neighbours[a].append({"id": b, "distance": distance, "bbox_overlap_area": overlap})
        neighbours[b].append({"id": a, "distance": distance, "bbox_overlap_area": overlap})
    length, height = result["solution"]["strip_width"], job["strip_height"]
    sections = []
    for index in range(16):
        left, right = length*index/16, length*(index+1)/16
        stripe = box(left, 0, right, height)
        sections.append({"x_range": [left, right], "fill": sum(s.intersection(stripe).area for s in shapes.values()) / stripe.area})
    return {"metrics": layout_metrics(job, result),
            "instances": [{"id": key, **p, "bounds": list(shapes[key].bounds),
                           "neighbours": sorted(neighbours[key], key=lambda n: n["distance"])[:6]} for key, p in found.items()],
            "rightmost_instances": sorted(found, key=lambda key: shapes[key].bounds[2], reverse=True)[:10],
            "strip_sections": sections}


def check_layout(job, result):
    """Small original-part acceptance check, with tolerance for Sparrow's f32 poses."""
    items = {i["id"]: i for i in job["items"]}
    placements = result["solution"]["layout"]["placed_items"]
    counts = Counter(p["item_id"] for p in placements)
    if counts != Counter({i["id"]: i["demand"] for i in job["items"] if i["demand"]}):
        raise ValueError("Layout does not contain the requested original inventory")
    length, height = result["solution"]["strip_width"], job["strip_height"]
    clearance = job.get("min_item_separation", 0)
    if not all(math.isfinite(x) and x > 0 for x in [length, height]):
        raise ValueError("Invalid strip dimensions")
    if not math.isfinite(clearance) or clearance < 0:
        raise ValueError("Invalid clearance")
    global_tolerance = max(1, length, height) * 2e-6
    shapes, tolerances = [], []
    for p in placements:
        item, pose = items[p["item_id"]], p["transformation"]
        if not all(math.isfinite(v) for v in [pose["rotation"], *pose["translation"]]):
            raise ValueError("Nonfinite placement")
        if not legal_pose(item, pose):
            raise ValueError(f"Forbidden orientation for part {item['id']}")
        original = polygon(item["shape"])
        bx0, by0, bx1, by1 = original.bounds
        tolerance = min(global_tolerance, max(bx1-bx0, by1-by0) * 2e-6,
                        original.area / original.length * 1e-3)
        shape = placed(original, pose)
        x0, y0, x1, y1 = shape.bounds
        if min(x0, y0, length-x1, height-y1) < clearance - tolerance:
            raise ValueError(f"Part {item['id']} violates strip bounds or boundary clearance")
        shapes.append(shape)
        tolerances.append(tolerance)
    for i, j in combinations(range(len(shapes)), 2):
        a, b = shapes[i], shapes[j]
        tolerance = min(tolerances[i], tolerances[j])
        area_tolerance = min(min(a.area, b.area) * 1e-6, min(a.length, b.length) * tolerance)
        if a.intersection(b).area > area_tolerance or a.distance(b) < clearance - tolerance:
            raise ValueError("Original parts overlap or violate clearance")
    return {"valid": True, "part_count": len(shapes), "tolerance": max(tolerances),
            "minimum_part_tolerance": min(tolerances), "overlap_area_fraction_cap": 1e-6,
            "checks": ["inventory", "orientations", "bounds", "overlap", "clearance"]}


def layout_metrics(job, result):
    area = sum(polygon(i["shape"]).area * i["demand"] for i in job["items"])
    length = result["solution"]["strip_width"]
    return {"strip_length": length, "utilization": area / (length * job["strip_height"]),
            "original_area": area, "part_count": len(result["solution"]["layout"]["placed_items"])}


def paint(ax, shape, color, label=None, labelsize=9):
    from matplotlib.patches import Polygon as Patch
    ax.add_patch(Patch(list(shape.exterior.coords), facecolor=color, edgecolor="#34435d", linewidth=0.7, alpha=0.85))
    if label:
        point = shape.representative_point()
        ax.text(point.x, point.y, label, ha="center", va="center", fontsize=labelsize,
                bbox={"facecolor": "white", "edgecolor": "none", "alpha": 0.75, "pad": 1})


def pyplot():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    return plt


def plot_layout(ax, job, result, title=None):
    plt = pyplot()
    from matplotlib.patches import Patch, Rectangle
    items = {i["id"]: i for i in job["items"]}
    placements = result["solution"]["layout"]["placed_items"]
    counts = Counter(p["item_id"] for p in placements)
    if counts != Counter({i["id"]: i["demand"] for i in job["items"] if i["demand"]}):
        raise ValueError("Final solution does not contain the requested inventory")
    metrics = layout_metrics(job, result)
    length, height = result["solution"]["strip_width"], job["strip_height"]
    utilization = metrics["utilization"]
    colors = {key: plt.get_cmap("tab10")(index % 10) for index, key in enumerate(sorted(items))}
    ax.add_patch(Rectangle((0, 0), length, height, facecolor="white", edgecolor="#34435d"))
    copies = Counter()
    for p in placements:
        key = p["item_id"]
        copies[key] += 1
        paint(ax, placed(polygon(items[key]["shape"]), p["transformation"]), colors[key], f"{key}:{copies[key]}",
              labelsize=6 if len(placements) > 30 else 9)
    ax.set(xlim=(-length * 0.02, length * 1.02), ylim=(-height * 0.03, height * 1.03),
           xlabel=f"Strip length ({job.get('units', 'input units')})", ylabel="Fixed strip height",
           title=f"{title or job['name']}  ·  length {length:.4f}  ·  utilization {utilization:.2%}")
    ax.set_aspect("equal")
    ax.legend(handles=[Patch(facecolor=colors[i], label=f"Part {i} × {counts[i]}") for i in sorted(counts)],
              bbox_to_anchor=(1.02, 1), loc="upper left", frameon=False, title="Labels: type:copy")
    return metrics


def draw_layout(job, result, path):
    plt = pyplot()
    fig, ax = plt.subplots(figsize=(11, 5))
    metrics = plot_layout(ax, job, result)
    fig.tight_layout()
    fig.savefig(path, dpi=160, bbox_inches="tight")
    plt.close(fig)
    return metrics


def draw_comparison(job, before, after, path, caption):
    import textwrap
    plt = pyplot()
    fig, axes = plt.subplots(2, 1, figsize=(14, 7))
    for ax, result, title in zip(axes, [before, after], ["Before · stock Sparrow", "After · retained best"]):
        plot_layout(ax, job, result, title)
    xmax = max(ax.get_xlim()[1] for ax in axes)
    for ax in axes:
        ax.set_xlim(axes[0].get_xlim()[0], xmax)
    fig.suptitle(f"{job['name']} · original-part layouts at the same scale", fontsize=14)
    fig.text(0.03, 0.015, textwrap.fill(caption, 155), fontsize=9, va="bottom")
    fig.tight_layout(rect=(0, 0.12, 1, 0.94))
    fig.savefig(path, dpi=160, bbox_inches="tight")
    plt.close(fig)


def draw_pairs(job, pairs, path):
    plt = pyplot()
    from matplotlib.patches import Patch
    candidates = pairs["candidates"]
    columns = min(2, len(candidates))
    rows = math.ceil(len(candidates) / columns)
    fig, axes = plt.subplots(rows, columns, figsize=(6 * columns, 4.3 * rows), squeeze=False)
    item = next(i for i in job["items"] if i["id"] == pairs["item_id"])
    shape = polygon(item["shape"])
    for ax, candidate in zip(axes.flat, candidates):
        envelope = polygon(candidate["envelope"])
        x, y = envelope.exterior.xy
        ax.fill(x, y, facecolor="#f0f3f8", edgecolor="#64748b", linestyle="--", linewidth=1)
        for index, member in enumerate(candidate["members"]):
            paint(ax, placed(shape, member["transformation"]), ["#4387b8", "#f39b45"][index], f"{item['id']}:{index + 1}")
        ax.autoscale_view()
        ax.set_aspect("equal")
        ax.set_title(f"{candidate['id']}  ·  envelope fill {candidate['envelope_fill']:.1%}\n"
                     f"{candidate['width']:.3f} × {candidate['height']:.3f}  ·  {candidate['rows_in_strip']} rows fit")
        ax.set_xlabel(job.get("units", "input units"))
    for ax in list(axes.flat)[len(candidates):]:
        ax.axis("off")
    fig.suptitle(f"Legal contact samples · part {item['id']} · clearance {pairs['clearance']:g}", fontsize=14)
    fig.legend(handles=[Patch(facecolor="#4387b8", label="First copy"), Patch(facecolor="#f39b45", label="Second copy"),
                        Patch(facecolor="#f0f3f8", edgecolor="#64748b", linestyle="--", label="Conservative envelope")],
               loc="lower center", ncol=3, frameon=False)
    fig.tight_layout(rect=(0, 0.05, 1, 0.94))
    fig.savefig(path, dpi=160, bbox_inches="tight")
    plt.close(fig)
