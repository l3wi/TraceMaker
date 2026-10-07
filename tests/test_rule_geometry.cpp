// SPDX-License-Identifier: GPL-3.0-or-later
// Source-pinned expectations: KiCad 10.0.3 pcbnew/pcbexpr_functions.cpp
// (area/courtyard predicates), footprint.cpp (5 um courtyard deflation),
// libs/kimath/src/geometry/shape_poly_set.cpp (miter limits 10/2).
// These are permanent unit cases, not claims of an exercised CLI oracle.
#include <catch2/catch_test_macros.hpp>

#include <algorithm>
#include <string>

#include "drc/rule_geometry.hpp"
#include "io/kicad/board_reader.hpp"

using namespace tmk;
namespace {
using geom::Point;
using geom::Shape;
using drc::CopperItem;
constexpr Coord mm = 1'000'000;

std::vector<Point> rectangle(Coord x0, Coord y0, Coord x1, Coord y1) {
  return {{x0, y0}, {x1, y0}, {x1, y1}, {x0, y1}};
}
CopperItem item(Shape shape, model::LayerMask layers = 1) {
  CopperItem result;
  result.layers = layers;
  result.box = shape.box;
  result.pos = shape.pts.front();
  result.shapes.push_back(std::move(shape));
  return result;
}
model::Zone zone(std::string name, std::vector<std::vector<Point>> rings, model::LayerMask layers = 1) {
  model::Zone result;
  result.name = std::move(name);
  result.outline = std::move(rings);
  result.copper = layers;
  result.rule_area = true;
  return result;
}
void courtyard(model::Board& board, std::size_t fp, std::vector<Point> ring, const char* layer) {
  model::Graphic graphic;
  graphic.kind = model::Graphic::Kind::Poly;
  graphic.pts = std::move(ring);
  graphic.layer = layer;
  graphic.footprint = static_cast<int>(fp);
  board.footprints[fp].graphics.push_back(static_cast<int>(board.graphics.size()));
  board.graphics.push_back(std::move(graphic));
}
}  // namespace

TEST_CASE("area collision uses effective shape, common layers and the 500 nm deflated outline", "[rules][geom]") {
  model::Board board;
  board.zones.push_back(zone("target", {rectangle(0, 0, 10 * mm, 10 * mm)}));
  const drc::RuleGeometry geometry(board);
  CHECK(geometry.warnings().empty());
  CHECK_FALSE(geometry.area(item(Shape::point({-mm, 5 * mm}, mm)), "target", false));  // merely touching
  CHECK_FALSE(geometry.area(item(Shape::point({-mm + 499, 5 * mm}, mm)), "target", false));
  CHECK_FALSE(geometry.area(item(Shape::point({-mm + 500, 5 * mm}, mm)), "target", false));  // tangent deflated boundary
  CHECK(geometry.area(item(Shape::point({-mm + 501, 5 * mm}, mm)), "target", false));
  CHECK(geometry.area(item(Shape::segment({-2 * mm, 5 * mm}, {2 * mm, 5 * mm}, 100'000)), "tar*", false));
  CHECK_FALSE(geometry.area(item(Shape::point({5 * mm, 5 * mm}, mm), 2), "target", false));
  CHECK(geometry.area(item(Shape::point({5 * mm, 5 * mm}, mm), 3), "target", false));
  CHECK_FALSE(geometry.area(item(Shape::point({5 * mm, 5 * mm}, mm)), "missing", false));
}

TEST_CASE("area holes and disjoint rings are independent of winding and do not become solid", "[rules][geom]") {
  model::Board board;
  auto hole = rectangle(3 * mm, 3 * mm, 7 * mm, 7 * mm);
  std::reverse(hole.begin(), hole.end());
  board.zones.push_back(zone("islands", {rectangle(0, 0, 10 * mm, 10 * mm), hole,
                                          rectangle(20 * mm, 0, 25 * mm, 5 * mm)}));
  const drc::RuleGeometry geometry(board);
  CHECK_FALSE(geometry.area(item(Shape::point({5 * mm, 5 * mm}, mm)), "islands", false));
  CHECK(geometry.area(item(Shape::point({22 * mm, 2 * mm}, mm)), "islands", false));
  CHECK_FALSE(geometry.area(item(Shape::point({15 * mm, 2 * mm}, mm)), "islands", false));
  CHECK_FALSE(geometry.area(item(Shape::point({5 * mm, 5 * mm}, 2 * mm)), "islands", false));
  CHECK_FALSE(geometry.area(item(Shape::point({5 * mm, 5 * mm}, 2 * mm + 500)), "islands", false));
  CHECK(geometry.area(item(Shape::point({5 * mm, 5 * mm}, 2 * mm + 501)), "islands", false));
  CHECK_FALSE(geometry.area(item(Shape::polygon(rectangle(2 * mm, 2 * mm, 8 * mm, 8 * mm))), "islands", true));
}

TEST_CASE("whole geometry enclosure rejects concave excursions between enclosed vertices", "[rules][geom]") {
  model::Board board;
  // A square with a narrow notch opening from its top; all bbox corners of the item are inside.
  board.zones.push_back(zone("notched", {{{0, 0}, {10 * mm, 0}, {10 * mm, 10 * mm}, {6 * mm, 10 * mm},
                                          {6 * mm, 3 * mm}, {4 * mm, 3 * mm}, {4 * mm, 10 * mm}, {0, 10 * mm}}}));
  const drc::RuleGeometry geometry(board);
  CHECK_FALSE(geometry.area(item(Shape::segment({2 * mm, 5 * mm}, {8 * mm, 5 * mm}, 50'000)), "notched", true));
  CHECK_FALSE(geometry.area(item(Shape::polygon(rectangle(mm, mm, 9 * mm, 9 * mm))), "notched", true));
  CHECK(geometry.area(item(Shape::segment({mm, mm}, {9 * mm, mm}, 100'000)), "notched", true));
  CHECK(geometry.area(item(Shape::point({mm, mm}, mm)), "notched", true));  // tangent original boundary
  CHECK_FALSE(geometry.area(item(Shape::point({mm, mm}, mm + 1)), "notched", true));
  CHECK(geometry.area(item(Shape::segment({2 * mm, 5 * mm}, {8 * mm, 5 * mm}, 50'000)), "notched", false));
}

TEST_CASE("zone predicates use fill for area intersection but all outline rings for enclosure and courtyard", "[rules][geom]") {
  model::Board board;
  board.zones.push_back(zone("area", {rectangle(0, 0, 10 * mm, 10 * mm)}));
  board.zones.push_back(zone("source", {rectangle(-mm, -mm, 11 * mm, 11 * mm), rectangle(2 * mm, 2 * mm, 8 * mm, 8 * mm)}));
  auto fill = item(Shape::polygon(rectangle(mm, mm, 2 * mm, 2 * mm)));
  fill.kind = drc::ItemKind::Zone;
  fill.index = 1;
  board.zones[1].fills.emplace_back(0, fill.shapes.front().pts);
  const drc::RuleGeometry geometry(board);
  CHECK(geometry.area(fill, "area", false));
  CHECK_FALSE(geometry.area(fill, "area", true));
  CHECK_FALSE(geometry.area(fill, "source", false));  // self exclusion
  CHECK_FALSE(geometry.area(fill, "A", false));
}

TEST_CASE("UUID area selectors preserve absolute footprint-area coordinates on rotated flipped parts", "[rules][geom]") {
  const auto document = sexpr::Document::parse(
      "(kicad_pcb (version 20240108) (generator pcbnew)"
      "(layers (0 F.Cu signal) (31 B.Cu signal))"
      "(footprint Lib:Part (layer B.Cu) (at 10 20 90)"
      "(property Reference U7 (at 0 0) (layer B.SilkS))"
      "(zone (net 0) (layer B.Cu) (name local) (uuid 11111111-2222-3333-4444-555555555555)"
      "(polygon (pts (xy 10 20) (xy 14 20) (xy 14 22) (xy 10 22))))))");
  const auto board = io::read_board(document);
  REQUIRE(board.zones.size() == 1);
  CHECK(board.zones[0].uuid == "11111111-2222-3333-4444-555555555555");
  const drc::RuleGeometry geometry(board);
  const Point centre{12 * mm, 21 * mm};
  CHECK(geometry.area(item(Shape::point(centre, 100'000), 2), board.zones[0].uuid, false));
  CHECK_FALSE(geometry.area(item(Shape::point({2 * mm, mm}, 100'000), 2), "local", false));
}

TEST_CASE("courtyard predicates preserve concavity, side swaps, library selectors and no layer restriction", "[rules][geom]") {
  model::Board board;
  board.footprints.resize(1);
  auto& fp = board.footprints[0];
  fp.reference = "U1";
  fp.lib_id = "Package:Part";
  fp.back = true;
  courtyard(board, 0, rectangle(20 * mm, 0, 25 * mm, 5 * mm), "F.CrtYd");
  courtyard(board, 0, {{0, 0}, {10 * mm, 0}, {10 * mm, 2 * mm}, {2 * mm, 2 * mm}, {2 * mm, 10 * mm}, {0, 10 * mm}}, "B.CrtYd");
  const drc::RuleGeometry geometry(board);
  const auto back = item(Shape::point({mm, mm}, 100'000), 1);
  CHECK(geometry.courtyard(back, "U*"));
  CHECK(geometry.courtyard(back, "Package:*", 1));  // front variant selects physical back on flipped fp
  CHECK_FALSE(geometry.courtyard(back, "U1", 2));
  CHECK_FALSE(geometry.courtyard(item(Shape::point({5 * mm, 5 * mm}, 100'000)), "U1"));  // not convex hull
  const auto front = item(Shape::point({22 * mm, 2 * mm}, 100'000), 2);
  CHECK(geometry.courtyard(front, "U1", 2));
  CHECK_FALSE(geometry.courtyard(front, "U1", 1));
  CHECK_FALSE(geometry.courtyard(item(Shape::point({-mm, mm}, mm)), "U1"));  // 5 um deflation
  CHECK_FALSE(geometry.courtyard(item(Shape::point({-mm + 5'000, mm}, mm)), "U1"));
  CHECK(geometry.courtyard(item(Shape::point({-mm + 5'001, mm}, mm)), "U1"));
}

TEST_CASE("courtyards reconstruct reversed line chains and nested holes, never pad fallback", "[rules][geom]") {
  model::Board board;
  board.footprints.resize(2);
  board.footprints[0].reference = "U1";
  board.footprints[1].reference = "U2";
  const auto ring = rectangle(0, 0, 10 * mm, 10 * mm);
  for (std::size_t i = 0; i < ring.size(); ++i) {
    model::Graphic graphic;
    graphic.layer = "F.CrtYd";
    graphic.a = ring[(i + 1) % ring.size()];
    graphic.b = ring[i];
    board.footprints[0].graphics.push_back(static_cast<int>(board.graphics.size()));
    board.graphics.push_back(graphic);
  }
  courtyard(board, 0, rectangle(3 * mm, 3 * mm, 7 * mm, 7 * mm), "F.CrtYd");
  const drc::RuleGeometry geometry(board);
  CHECK(geometry.courtyard(item(Shape::point({mm, mm}, 100'000)), "U1"));
  CHECK_FALSE(geometry.courtyard(item(Shape::point({5 * mm, 5 * mm}, 100'000)), "U1"));
  CHECK_FALSE(geometry.courtyard(item(Shape::point({mm, mm}, 100'000)), "U2"));
  CHECK_FALSE(geometry.validate_courtyard("U2").empty());
  CHECK_FALSE(geometry.validate_courtyard("${Class:RF}").empty());
}

TEST_CASE("rotated and flipped courtyard graphics are consumed in board coordinates", "[rules][geom]") {
  const auto document = sexpr::Document::parse(
      "(kicad_pcb (version 20240108) (generator pcbnew)"
      "(layers (0 F.Cu signal) (31 B.Cu signal) (46 B.CrtYd user) (47 F.CrtYd user))"
      "(footprint Lib:Part (layer B.Cu) (at 10 20 45)"
      "(property Reference U1 (at 0 0) (layer B.SilkS))"
      "(fp_rect (start -2 -1) (end 2 1) (stroke (width 0.05) (type solid)) (fill none) (layer B.CrtYd))))");
  const auto board = io::read_board(document);
  const drc::RuleGeometry geometry(board);
  CHECK(geometry.courtyard(item(Shape::point({10 * mm, 20 * mm}, 100'000)), "U1", 1));
  CHECK_FALSE(geometry.courtyard(item(Shape::point({10 * mm, 20 * mm}, 100'000)), "U1", 2));
  const Point outside = board.footprints[0].pos + geom::rotate({0, 2 * mm}, 45);
  CHECK_FALSE(geometry.courtyard(item(Shape::point(outside, 100'000)), "U1"));
}

TEST_CASE("malformed area and open courtyard geometry have structural diagnostics", "[rules][geom]") {
  model::Board board;
  board.zones.push_back(zone("broken", {{{0, 0}, {10 * mm, 10 * mm}, {0, 10 * mm}, {10 * mm, 0}}}));
  board.footprints.resize(1);
  board.footprints[0].reference = "U1";
  model::Graphic graphic;
  graphic.layer = "F.CrtYd";
  graphic.a = {0, 0};
  graphic.b = {10 * mm, 0};
  board.footprints[0].graphics.push_back(0);
  board.graphics.push_back(graphic);
  const drc::RuleGeometry geometry(board);
  CHECK(geometry.warnings().size() == 2);
  CHECK_FALSE(geometry.validate_area("broken").empty());
  CHECK(geometry.validate_area("unrelated").empty());
  CHECK_FALSE(geometry.validate_courtyard("U1").empty());
  CHECK_FALSE(geometry.area(item(Shape::point({mm, mm}, mm)), "broken", false));
  CHECK_FALSE(geometry.courtyard(item(Shape::point({mm, 0}, mm)), "U1"));
}

TEST_CASE("prepared polygon predicates equal independently prepared linear reference", "[rules][geom][reference]") {
  model::Board board;
  board.zones.push_back(zone("target", {rectangle(0, 0, 10 * mm, 10 * mm), rectangle(3 * mm, 3 * mm, 7 * mm, 7 * mm)}));
  board.footprints.resize(1);
  board.footprints[0].reference = "U1";
  courtyard(board, 0, {{0, 0}, {10 * mm, 0}, {10 * mm, 2 * mm}, {2 * mm, 2 * mm}, {2 * mm, 10 * mm}, {0, 10 * mm}}, "F.CrtYd");
  const drc::RuleGeometry geometry(board);
  const auto area_ids = geometry.area_regions("tar*");
  const auto courtyard_ids = geometry.courtyard_regions("U*", 1);
  CHECK(geometry.area_regions("missing").empty());
  CHECK(geometry.courtyard_regions("missing").empty());
  for (int x = -2; x <= 12; ++x)
    for (int y = -2; y <= 12; ++y) {
      const std::vector<Shape> shapes{
          Shape::point({x * mm, y * mm}, 250'000),
          Shape::segment({x * mm, y * mm}, {(x + 3) * mm, (y + 1) * mm}, 100'000),
          Shape::polygon(rectangle(x * mm, y * mm, (x + 2) * mm, (y + 2) * mm))};
      for (const auto& shape : shapes) {
        const auto probe = item(shape);
        for (bool enclosure : {false, true}) {
          const bool reference = geometry.area_reference(probe, "target", enclosure);
          CHECK(geometry.area(probe, "target", enclosure) == reference);
          CHECK(geometry.area(probe, area_ids, enclosure) == reference);
        }
        const bool reference = geometry.courtyard_reference(probe, "U1", 1);
        CHECK(geometry.courtyard(probe, "U1", 1) == reference);
        CHECK(geometry.courtyard(probe, courtyard_ids) == reference);
      }
    }
}

TEST_CASE("courtyard circles, arcs and cubic curves form actual closed contours", "[rules][geom]") {
  model::Board board;
  board.footprints.resize(3);
  board.footprints[0].reference = "C1";
  board.footprints[1].reference = "A1";
  board.footprints[2].reference = "B1";
  auto add = [&](std::size_t fp, model::Graphic graphic) {
    graphic.layer = "F.CrtYd";
    graphic.footprint = static_cast<int>(fp);
    board.footprints[fp].graphics.push_back(static_cast<int>(board.graphics.size()));
    board.graphics.push_back(std::move(graphic));
  };
  model::Graphic circle;
  circle.kind = model::Graphic::Kind::Circle;
  circle.a = {0, 0};
  circle.b = {5 * mm, 0};
  add(0, circle);
  model::Graphic arc;
  arc.kind = model::Graphic::Kind::Arc;
  arc.a = {-5 * mm, 0};
  arc.c = {0, 5 * mm};
  arc.b = {5 * mm, 0};
  add(1, arc);
  model::Graphic arc_base;
  arc_base.a = arc.b;
  arc_base.b = arc.a;
  add(1, arc_base);
  model::Graphic curve;
  curve.kind = model::Graphic::Kind::Curve;
  curve.pts = {{-2 * mm, 0}, {-2 * mm, 2 * mm}, {2 * mm, 2 * mm}, {2 * mm, 0}};
  add(2, curve);
  model::Graphic curve_base;
  curve_base.a = curve.pts.back();
  curve_base.b = curve.pts.front();
  add(2, curve_base);
  const drc::RuleGeometry geometry(board);
  CHECK(geometry.warnings().empty());
  CHECK(geometry.courtyard(item(Shape::point({0, 0}, 100'000)), "C1"));
  CHECK_FALSE(geometry.courtyard(item(Shape::point({6 * mm, 0}, 100'000)), "C1"));
  CHECK(geometry.courtyard(item(Shape::point({0, 2 * mm}, 100'000)), "A1"));
  CHECK_FALSE(geometry.courtyard(item(Shape::point({0, -2 * mm}, 100'000)), "A1"));
  CHECK(geometry.courtyard(item(Shape::point({0, 500'000}, 100'000)), "B1"));
  CHECK_FALSE(geometry.courtyard(item(Shape::point({0, 2 * mm}, 100'000)), "B1"));
}

TEST_CASE("zone courtyard collision uses outline even when fill bbox is remote and holes remain empty", "[rules][geom]") {
  model::Board board;
  board.footprints.resize(1);
  board.footprints[0].reference = "U1";
  courtyard(board, 0, rectangle(0, 0, 2 * mm, 2 * mm), "F.CrtYd");
  board.zones.push_back(zone("source", {rectangle(-mm, -mm, 10 * mm, 10 * mm)}));
  auto fill = item(Shape::polygon(rectangle(8 * mm, 8 * mm, 9 * mm, 9 * mm)));
  fill.kind = drc::ItemKind::Zone;
  fill.index = 0;
  const drc::RuleGeometry geometry(board);
  CHECK(geometry.courtyard(fill, "U1"));
  CHECK(geometry.courtyard(fill, "U1") == geometry.courtyard_reference(fill, "U1"));
  board.zones[0].outline.push_back(rectangle(-500'000, -500'000, 3 * mm, 3 * mm));
  const drc::RuleGeometry holed_geometry(board);
  CHECK_FALSE(holed_geometry.courtyard(fill, "U1"));
}

TEST_CASE("thin areas vanish on deflation and enclosure checks every constituent shape", "[rules][geom]") {
  model::Board board;
  board.zones.push_back(zone("thin", {rectangle(0, 0, 800, mm)}));
  board.zones.push_back(zone("wide", {rectangle(0, 0, 10 * mm, 10 * mm)}));
  const drc::RuleGeometry geometry(board);
  CHECK_FALSE(geometry.area(item(Shape::point({400, mm / 2}, 100'000)), "thin", false));
  auto composite = item(Shape::point({mm, mm}, 100'000));
  composite.shapes.push_back(Shape::point({11 * mm, mm}, 100'000));
  composite.box.add(composite.shapes.back().box);
  CHECK_FALSE(geometry.area(composite, "wide", true));
  CHECK(geometry.area(composite, "wide", false));
}

TEST_CASE("named copper-zone targets use the outline, whereas unfilled source zones do not intersect areas", "[rules][geom]") {
  model::Board board;
  board.zones.push_back(zone("target", {rectangle(0, 0, 10 * mm, 10 * mm)}));
  board.zones[0].rule_area = false;
  board.zones[0].fills.emplace_back(0, rectangle(0, 0, mm, mm));
  board.zones.push_back(zone("unfilled", {rectangle(5 * mm, 5 * mm, 8 * mm, 8 * mm)}));
  const drc::RuleGeometry geometry(board);
  CHECK(geometry.area(item(Shape::point({6 * mm, 6 * mm}, 100'000)), "target", false));
  auto source = item(Shape::polygon(board.zones[1].outline.front()));
  source.kind = drc::ItemKind::Zone;
  source.index = 1;
  CHECK_FALSE(geometry.area(source, "target", false));
  CHECK(geometry.area(source, "target", true));
  CHECK_FALSE(geometry.area(source, "", false));
}

TEST_CASE("rounded rectangle courtyard corners are not replaced with rectangular copper bounds", "[rules][geom]") {
  const auto document = sexpr::Document::parse(
      "(kicad_pcb (version 20260101) (generator pcbnew)"
      "(layers (0 F.Cu signal) (31 B.Cu signal) (46 B.CrtYd user) (47 F.CrtYd user))"
      "(footprint Lib:Part (layer B.Cu) (at 10 20 45)"
      "(property Reference U1 (at 0 0) (layer B.SilkS))"
      "(fp_rect (start -2 -1) (end 2 1) (radius 0.8)"
      "(stroke (width 0.05) (type solid)) (fill none) (layer B.CrtYd))))");
  const auto board = io::read_board(document);
  REQUIRE(board.graphics.size() == 1);
  CHECK(board.graphics[0].corner_radius == 800'000);
  const drc::RuleGeometry geometry(board);
  CHECK(geometry.warnings().empty());
  CHECK(geometry.courtyard(item(Shape::point({10 * mm, 20 * mm}, 10'000)), "U1", 1));
  const Point corner = board.footprints[0].pos + geom::rotate({1'900'000, 900'000}, 45);
  CHECK_FALSE(geometry.courtyard(item(Shape::point(corner, 10'000)), "U1", 1));
}
