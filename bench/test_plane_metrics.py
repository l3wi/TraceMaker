# SPDX-License-Identifier: GPL-3.0-or-later
"""Run with python3 -m unittest discover -s bench -p 'test_*.py'."""
import math
import pathlib
import tempfile
import unittest

from plane_metrics import arc_geometry, clipped_area, copper_totals, parse, plane_nets, unconnected_split
from prepare_dac2020 import strip_routing


def rectangle(x1, y1, x2, y2):
    return [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]


def zone(x1, x2, extra='', layer='Ground', net=1):
    return f'''(zone (net {net}) (layer "{layer}") {extra}
        (polygon (pts (xy {x1} 0) (xy {x2} 0) (xy {x2} 10) (xy {x1} 10))))'''


class PlaneMetricsTests(unittest.TestCase):
    def test_union_overlap_clipping_and_holes(self):
        outline = [rectangle(0, 0, 10, 10), rectangle(2, 2, 4, 4)]
        groups = [[rectangle(-10, -10, 6, 20)], [rectangle(4, 0, 20, 10)]]
        self.assertAlmostEqual(clipped_area(groups, outline), 96)
        self.assertAlmostEqual(clipped_area([[rectangle(0, 0, 6, 10)]], outline), 56)

    def test_edge_intersections_split_slabs(self):
        outline = [rectangle(0, 0, 10, 10)]
        groups = [[[(0, 0), (10, 0), (0, 10)]], [[(10, 10), (0, 10), (10, 0)]]]
        self.assertAlmostEqual(clipped_area(groups, outline), 100)
        self.assertAlmostEqual(clipped_area([groups[0][0:1]], outline), 50)

    def test_zone_hole_is_not_filled_by_union(self):
        outline = [rectangle(0, 0, 10, 10)]
        groups = [[rectangle(0, 0, 10, 10), rectangle(2, 2, 8, 8)]]
        self.assertAlmostEqual(clipped_area(groups, outline), 64)

    def test_arcs_old_and_new(self):
        old = parse('(gr_arc (start 0 0) (end 1 0) (angle 90))')
        new = parse('(arc (start 1 0) (mid 0.7071067811865476 0.7071067811865476) (end 0 1))')
        for node in (old, new):
            first, last, integral, length = arc_geometry(node)
            self.assertAlmostEqual(length, math.pi / 2)
            self.assertAlmostEqual(integral, math.pi / 2)
            self.assertAlmostEqual(first[0], 1)
            self.assertAlmostEqual(last[1], 1)

    def test_parser_escaped_strings(self):
        self.assertEqual(parse(r'(net 1 "GND (\"analog\")")')[2], 'GND ("analog")')
        with self.assertRaises(ValueError):
            parse('(unclosed')

    def test_same_net_same_layer_threshold_and_copper(self):
        header = '''(kicad_pcb (layers (0 "Top" signal) (1 "Ground" power) (2 "Other" signal) (31 "Bottom" signal) (44 "Edge.Cuts" user))
            (net 0 "") (net 1 "GND") (net 2 "SIG")
            (gr_rect (start 0 0) (end 10 10) (layer "Edge.Cuts"))'''
        # Two disjoint zones qualify only together; duplicates must not add area.
        text = header + zone(0, 3) + zone(7, 10) + zone(0, 3)
        text += zone(0, 10, '(keepout (tracks allowed))', net=2)
        text += zone(0, 10, '(attr (teardrop (type padvia)))', net=2)
        text += zone(0, 10, layer='B.Mask', net=2)
        text += '(segment (start 0 0) (end 3 4) (net 1)) (via (net 2)) )'
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / 'board with spaces.kicad_pcb'
            path.write_text(text)
            planes = plane_nets(path)
            self.assertEqual(set(planes), {'GND'})
            self.assertEqual(planes['GND']['layer_coverage'], {'Ground': 0.6})
            self.assertEqual(copper_totals(path, planes), {'plane_vias': 0, 'signal_vias': 1,
                                                        'plane_track_mm': 5.0, 'signal_track_mm': 0.0})
            stripped = strip_routing(text)
            self.assertIn(zone(0, 3), stripped)
            self.assertNotIn('(segment ', stripped)
            path.write_text(header + zone(0, 3) + zone(7, 10, layer='Other') + ')')
            self.assertEqual(plane_nets(path), {})

    def test_unconnected_records_partition(self):
        records = [{'items': [{'description': 'Pad 1 [GND] of U1'}, {'description': 'Pad 2 [GND] of U2'}]},
                   {'items': [{'description': 'Pad 1 [SIG] of U1'}]}]
        self.assertEqual(unconnected_split(records, {'GND': {}}), {'plane_unconnected': 1, 'signal_unconnected': 1})


if __name__ == '__main__':
    unittest.main()
