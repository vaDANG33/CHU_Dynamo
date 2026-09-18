"""Tests du Python embarque avec un noyau geometrique minimal, sans Revit."""
import json
import math
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class Point:
    def __init__(self, x, y, z=0):
        self.X, self.Y, self.Z = x, y, z

    ByCoordinates = staticmethod(lambda *xyz: Point(*xyz))

    def Dispose(self):
        pass


class Line:
    def __init__(self, a, b):
        self.StartPoint, self.EndPoint = Point(*a), Point(*b)

    @staticmethod
    def ByStartPointEndPoint(a, b):
        return Line((a.X, a.Y, a.Z), (b.X, b.Y, b.Z))


def run(curves, minimum=10, tolerance=1, scale=1):
    # scale = nombre d'unites du projet dans un millimetre.
    units = types.SimpleNamespace(
        ConvertToInternalUnits=lambda value, unit: value,
        ConvertFromInternalUnits=lambda value, unit: value * scale)
    document = types.SimpleNamespace(GetUnits=lambda: types.SimpleNamespace(
        GetFormatOptions=lambda spec: types.SimpleNamespace(GetUnitTypeId=lambda: "project")))
    modules = {
        "clr": types.SimpleNamespace(AddReference=lambda name: None),
        "Autodesk.DesignScript.Geometry": types.SimpleNamespace(Line=Line, Point=Point),
        "Autodesk.Revit.DB": types.SimpleNamespace(UnitUtils=units,
            UnitTypeId=types.SimpleNamespace(Millimeters="mm"),
            SpecTypeId=types.SimpleNamespace(Length="length")),
        "RevitServices.Persistence": types.SimpleNamespace(DocumentManager=types.SimpleNamespace(
            Instance=types.SimpleNamespace(CurrentDBDocument=document))),
    }
    graph = json.loads((ROOT / "Mur_DWG-v3.dyn").read_text(encoding="utf-8"))
    code = next(n["Code"] for n in graph["Nodes"] if n["Id"] == "85c5a7bf21fc4c6f9f26805b52d6b861")
    env = {"IN": [curves, minimum, tolerance]}
    with patch.dict(sys.modules, modules):
        exec(compile(code, "MUR_01", "exec"), env)
    return env["OUT"]


def endpoints(line):
    return tuple((p.X, p.Y, p.Z) for p in (line.StartPoint, line.EndPoint))


class CleaningTests(unittest.TestCase):
    def test_embedded_source_matches(self):
        graph = json.loads((ROOT / "Mur_DWG-v3.dyn").read_text(encoding="utf-8"))
        code = next(n["Code"] for n in graph["Nodes"] if n["Id"] == "85c5a7bf21fc4c6f9f26805b52d6b861")
        self.assertEqual(code, (ROOT / "tools/mur01.py").read_text(encoding="utf-8"))

    def test_reversed_and_boundary_duplicates(self):
        lines, d = run([Line((0.49, 0, 0), (100.49, 0, 0)),
                        Line((100.51, 0, 0), (0.51, 0, 0))])
        self.assertEqual(len(lines), 1)
        self.assertEqual(d["Doublons supprimes"], 1)

    def test_euclidean_tolerance(self):
        lines, _ = run([Line((0, 0, 0), (100, 0, 0)),
                        Line((0, .8, .8), (100, .8, .8))])
        self.assertEqual(len(lines), 2)

    def test_overlap_chain_and_containment(self):
        lines, d = run([Line((0, 0, 0), (20, 0, 0)),
                        Line((15, 0, 0), (35, 0, 0)),
                        Line((30, 0, 0), (50, 0, 0)),
                        Line((2, 0, 0), (12, 0, 0))])
        self.assertEqual([endpoints(x) for x in lines], [((0, 0, 0), (50, 0, 0))])
        self.assertEqual(d["Segments absorbes par fusion"], 3)

    def test_fragments_filtered_after_union(self):
        lines, _ = run([Line((0, 0, 0), (6, 0, 0)), Line((6, 0, 0), (12, 0, 0))])
        self.assertEqual(len(lines), 1)
        self.assertEqual(endpoints(lines[0])[1][0], 12)

    def test_real_gap_and_parallel_faces_preserved(self):
        lines, _ = run([Line((0, 0, 0), (20, 0, 0)),
                        Line((20.5, 0, 0), (40, 0, 0)),
                        Line((10, .5, 0), (30, .5, 0))])
        self.assertEqual(len(lines), 3)

    def test_crossings_and_different_elevations(self):
        lines, _ = run([Line((0, 0, 0), (20, 0, 0)),
                        Line((10, -10, 0), (10, 10, 0)),
                        Line((0, 0, 5), (20, 0, 5))])
        self.assertEqual(len(lines), 3)

    def test_diagonal_3d_and_large_coordinates(self):
        a = (1e8, -2e8, 1e7)
        def p(t):
            return tuple(x + t * d for x, d in zip(a, (1, 2, 3)))
        lines, _ = run([Line(p(0), p(20)), Line(p(10), p(30))])
        self.assertEqual(len(lines), 1)
        for actual, expected in zip(endpoints(lines[0]), (p(0), p(30))):
            for x, y in zip(actual, expected):
                self.assertAlmostEqual(x, y, places=6)

    def test_units_meters(self):
        lines, _ = run([Line((0, 0, 0), (.006, 0, 0)),
                        Line((.006, 0, 0), (.012, 0, 0))], scale=.001)
        self.assertEqual(len(lines), 1)
        self.assertAlmostEqual(endpoints(lines[0])[1][0], .012)

    def test_zero_tolerance(self):
        lines, d = run([Line((0, 0, 0), (100, 0, 0)),
                        Line((100, 0, 0), (0, 0, 0)),
                        Line((0, .001, 0), (100, .001, 0))], tolerance=0)
        self.assertEqual(len(lines), 2)
        self.assertEqual(d["Doublons supprimes"], 1)

    def test_invalid_and_nested_inputs(self):
        lines, d = run([None, [[Line((0, 0, 0), (20, 0, 0))]],
                        "arc", Line((0, 0, 0), (math.nan, 0, 0))])
        self.assertEqual(len(lines), 1)
        self.assertEqual(d["Erreurs"], 1)
        self.assertEqual(d["Courbes non lineaires exclues"], 1)
        self.assertEqual(run([])[0], [])
        with self.assertRaises(ValueError):
            run([], tolerance=-1)

    def test_deterministic_and_idempotent(self):
        source = [Line((0, 0, 0), (20, 0, 0)), Line((15, 0, 0), (35, 0, 0)),
                  Line((0, 20, 0), (30, 20, 0))]
        first, _ = run(source)
        reverse, _ = run(list(reversed(source)))
        second, _ = run(first)
        normalize = lambda lines: sorted(endpoints(x) for x in lines)
        self.assertEqual(normalize(first), normalize(reverse))
        self.assertEqual(normalize(first), normalize(second))

    def test_large_parallel_set_uses_index(self):
        lines, d = run([Line((0, y * 10, 0), (100, y * 10, 0)) for y in range(3000)])
        self.assertEqual(len(lines), 3000)
        self.assertLess(d["Comparaisons supports"], 3000)


if __name__ == "__main__":
    unittest.main()
