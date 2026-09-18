# -*- coding: utf-8 -*-
"""MUR_01: deduplication spatiale et union des intervalles colineaires.

IN: courbes, longueur minimum (mm), tolerance des doublons (mm).
OUT: lignes, diagnostic. Aucun vide reel n'est comble par la fusion.
Les coordonnees DesignScript restent dans les unites du projet.
"""
import math
from itertools import product
import clr

clr.AddReference("ProtoGeometry")
from Autodesk.DesignScript.Geometry import Line, Point
clr.AddReference("RevitAPI")
from Autodesk.Revit.DB import UnitUtils, UnitTypeId, SpecTypeId
clr.AddReference("RevitServices")
from RevitServices.Persistence import DocumentManager


def parameter(index, default):
    value = IN[index] if len(IN) > index else None
    value = default if value is None else float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError("IN[{}] doit etre un nombre fini positif ou nul.".format(index))
    return value


min_length_mm = parameter(1, 10.0)
tolerance_mm = parameter(2, 1.0)
doc = DocumentManager.Instance.CurrentDBDocument
unit = doc.GetUnits().GetFormatOptions(SpecTypeId.Length).GetUnitTypeId()


def mm_to_project(value):
    return UnitUtils.ConvertFromInternalUnits(
        UnitUtils.ConvertToInternalUnits(value, UnitTypeId.Millimeters), unit)


min_length = mm_to_project(min_length_mm)
tolerance = mm_to_project(tolerance_mm)


def sub(a, b):
    return tuple(x - y for x, y in zip(a, b))


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def distance(a, b):
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def xyz(p):
    return (float(p.X), float(p.Y), float(p.Z))


# Aplatissement iteratif: pas de limite de recursion, accepte les listes .NET.
stack = [IN[0]]
records = []
errors = []
initial = detected = excluded = degenerate = 0
while stack:
    item = stack.pop()
    if item is None:
        continue
    if not isinstance(item, Line):
        if not isinstance(item, (str, bytes)) and hasattr(item, "__iter__"):
            stack.extend(reversed(list(item)))
            continue
        initial += 1
        excluded += 1
        continue
    initial += 1
    detected += 1
    try:
        a, b = xyz(item.StartPoint), xyz(item.EndPoint)
        if not all(math.isfinite(x) for x in a + b):
            raise ValueError("Coordonnees non finies")
        if b < a:
            a, b = b, a
        length = distance(a, b)
        if length == 0:
            degenerate += 1
            continue
        records.append({"a": a, "b": b, "curve": item, "length": length})
    except Exception as ex:
        errors.append("Lecture ligne {}: {}".format(initial, ex))

# Priorite aux lignes longues, puis ordre geometrique reproductible.
records.sort(key=lambda r: (-r["length"], r["a"], r["b"]))
neighbors3 = tuple(product((-1, 0, 1), repeat=3))
endpoint_index = {}
unique = []
duplicates = duplicate_tests = 0


def cell(p, size):
    return tuple(int(math.floor(x / size)) for x in p)


for r in records:
    candidates = set()
    if tolerance > 0:
        key = cell(r["a"], tolerance)
        for delta in neighbors3:
            candidates.update(endpoint_index.get(tuple(x + y for x, y in zip(key, delta)), ()))
    else:
        candidates.update(endpoint_index.get(r["a"], ()))
    duplicate = False
    for index in sorted(candidates):
        other = unique[index]
        duplicate_tests += 1
        if ((distance(r["a"], other["a"]) <= tolerance and
             distance(r["b"], other["b"]) <= tolerance) or
            (distance(r["a"], other["b"]) <= tolerance and
             distance(r["b"], other["a"]) <= tolerance)):
            duplicate = True
            break
    if duplicate:
        duplicates += 1
        continue
    index = len(unique)
    unique.append(r)
    # Indexer les deux extremites evite les instabilites du sens canonique.
    for p in (r["a"], r["b"]):
        key = cell(p, tolerance) if tolerance > 0 else p
        endpoint_index.setdefault(key, set()).add(index)

# Supports 3D: index par axe dominant et deux intersections avec son plan zero.
# Le repere local limite les pertes de precision des DWG georeferences.
origin = tuple(min(r["a"][i] for r in unique) for i in range(3)) if unique else (0, 0, 0)
span = max((distance(p, origin) for r in unique for p in (r["a"], r["b"])), default=0)
magnitude = max((abs(x) for r in unique for p in (r["a"], r["b"]) for x in p), default=0)
epsilon = max(mm_to_project(1e-7), magnitude * 2e-15)
angle_epsilon = 1e-10
support_cell = 8 * (epsilon + span * angle_epsilon)
support_index = {}
groups = []
support_tests = 0


def support_distance(p, group):
    v = sub(p, group["origin"])
    t = dot(v, group["direction"])
    return math.sqrt(sum((v[i] - t * group["direction"][i]) ** 2 for i in range(3)))


for r in unique:
    direction = tuple(x / r["length"] for x in sub(r["b"], r["a"]))
    dominant = max(range(3), key=lambda i: abs(direction[i]))
    p = sub(r["a"], origin)
    others = [i for i in range(3) if i != dominant]
    intercept = tuple(p[i] - p[dominant] * direction[i] / direction[dominant] for i in others)
    key = cell(intercept, support_cell)
    candidates = set()
    for delta in product((-1, 0, 1), repeat=2):
        candidates.update(support_index.get((dominant, key[0] + delta[0], key[1] + delta[1]), ()))
    selected = None
    for index in sorted(candidates):
        group = groups[index]
        support_tests += 1
        d = group["direction"]
        # Comparaison vectorielle, sans acos ni soustraction de cosinus proches de 1.
        parallel = min(distance(direction, d), distance(direction, tuple(-x for x in d)))
        if parallel <= angle_epsilon and max(support_distance(r["a"], group), support_distance(r["b"], group)) <= epsilon:
            selected = group
            break
    if selected is None:
        selected = {"origin": r["a"], "direction": direction, "intervals": []}
        index = len(groups)
        groups.append(selected)
        support_index.setdefault((dominant,) + key, []).append(index)
    t0, t1 = sorted(dot(sub(p, selected["origin"]), selected["direction"]) for p in (r["a"], r["b"]))
    selected["intervals"].append((t0, t1, r))

final_lines = []
removed_short = merged = construction_failures = 0


def emit(group, start, end, members):
    global removed_short, construction_failures
    if end - start < min_length:
        removed_short += 1
        return
    if len(members) == 1 and xyz(members[0]["curve"].StartPoint) == members[0]["a"]:
        final_lines.append(members[0]["curve"])
        return
    points = []
    try:
        for t in (start, end):
            coords = tuple(group["origin"][i] + t * group["direction"][i] for i in range(3))
            points.append(Point.ByCoordinates(*coords))
        final_lines.append(Line.ByStartPointEndPoint(*points))
    except Exception as ex:
        construction_failures += 1
        errors.append("Construction: {}".format(ex))
        # Preserver les geometries sources si le noyau refuse la reconstruction.
        final_lines.extend(r["curve"] for r in members if r["length"] >= min_length)
    finally:
        for p in points:
            p.Dispose()


for group in groups:
    intervals = sorted(group["intervals"], key=lambda item: (item[0], item[1]))
    start, end, record = intervals[0]
    members = [record]
    for a, b, record in intervals[1:]:
        if a <= end + epsilon:
            end = max(end, b)
            members.append(record)
            merged += 1
        else:
            emit(group, start, end, members)
            start, end, members = a, b, [record]
    emit(group, start, end, members)

diagnostic = {
    "Version": "MUR_01_V2_UnionColineaire",
    "Courbes initiales": initial,
    "Lignes detectees": detected,
    "Doublons supprimes": duplicates,
    "Lignes courtes supprimees": removed_short,
    "Lignes finales": len(final_lines),
    "Longueur mini mm": min_length_mm,
    "Tolerance mm": tolerance_mm,
    "Courbes non lineaires exclues": excluded,
    "Lignes de longueur nulle": degenerate,
    "Segments absorbes par fusion": merged,
    "Supports colineaires": len(groups),
    "Comparaisons doublons": duplicate_tests,
    "Comparaisons supports": support_tests,
    "Echecs reconstruction": construction_failures,
    "Erreurs": len(errors),
    "Details erreurs": errors,
}
OUT = final_lines, diagnostic
