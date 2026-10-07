// SPDX-License-Identifier: GPL-3.0-or-later
#include <catch2/catch_test_macros.hpp>
#include <algorithm>
#include <cmath>
#include <numbers>

#include "io/kicad/board_reader.hpp"
#include "io/kicad/board_editor.hpp"
#include "drc/connectivity.hpp"
#include "route/obstacles.hpp"
#include "route/plane_map.hpp"
#include "route/router.hpp"

using namespace tmk;
namespace {
std::string plane(const char* layer, int net, const char* name, const char* pts) {
  return std::string("(zone (net ") + std::to_string(net) + ") (net_name \"" + name + "\") (layer \"" + layer +
      "\") (hatch edge 0.5) (connect_pads (clearance 0.2)) (min_thickness 0.2) (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3)) " +
      "(polygon (pts " + pts + ")) (filled_polygon (layer \"" + layer + "\") (pts " + pts + ")))\n";
}
std::string board_text(const std::string& extra, const char* pad_layer = "F.Cu") {
  return std::string("(kicad_pcb (version 20240108) (generator \"pcbnew\")\n") +
      "(layers (0 \"F.Cu\" signal) (1 \"In1.Cu\" signal) (2 \"In2.Cu\" signal) (31 \"B.Cu\" signal) (44 \"Edge.Cuts\" user))\n" +
      "(net 0 \"\") (net 1 \"SIG\") (net 2 \"GND\")\n" +
      "(footprint \"R\" (layer \"F.Cu\") (at 4 5) (property \"Reference\" \"R1\" (at 0 0) (layer \"F.SilkS\")) " +
      "(pad \"1\" smd rect (at 0 0) (size 0.6 0.6) (layers \"" + pad_layer + "\") (net 2 \"GND\")))\n" +
      "(gr_rect (start 0 0) (end 12 10) (layer \"Edge.Cuts\") (stroke (width 0.1) (type solid)))\n" + extra + ")\n";
}
model::Board parse(const std::string& text) {
  auto doc = sexpr::Document::parse(text);
  return io::read_board(doc);
}
const char* kPts = "(xy 1 1) (xy 11 1) (xy 11 9) (xy 1 9)";
const char* kArea = "(zone (net 0) (net_name \"\") (layer \"In1.Cu\") (hatch edge 0.5) (min_thickness 0.2) "
    "(keepout (tracks not_allowed) (vias allowed) (pads allowed) (copperpour allowed) (footprints allowed)) "
    "(polygon (pts (xy 6 2) (xy 9 2) (xy 9 8) (xy 6 8))))\n";
}

TEST_CASE("soft zones preserve fixed via parity and rule areas, holes and edges", "[route][soft-zones]") {
  auto b = parse(board_text(plane("In1.Cu", 2, "GND", kPts) + plane("In2.Cu", 1, "SIG", kPts) + kArea +
      "(footprint \"H\" (layer \"F.Cu\") (at 10 5) (pad \"\" np_thru_hole circle (at 0 0) (size 1 1) (drill 1) (layers \"*.Cu\" \"*.Mask\")))"));
  model::DesignRules rules;
  rules.classes.emplace_back();
  model::CustomRule physical;
  physical.name = "via hole clear of fixed copper";
  model::Constraint constraint;
  constraint.type = "physical_hole_clearance";
  constraint.min = 100'000;
  physical.constraints.push_back(constraint);
  rules.custom.push_back(physical);
  for (bool soft : {false, true}) {
    route::Obstacles obs(b, rules, soft);
    for (int x = -1; x <= 25; ++x)
      for (int y = -1; y <= 21; ++y)
        for (model::NetId net : {1, 2}) {
          const geom::Point p{x * 500'000, y * 500'000};
          CHECK(obs.fixed_via_code(p, 600'000, 300'000, 0, net) == obs.fixed_via_code_reference(p, 600'000, 300'000, 0, net));
        }
    CHECK(obs.segment_ok({2'000'000, 3'000'000}, {5'000'000, 3'000'000}, 1, 200'000, 1) == soft);
    CHECK(obs.via_ok({3'000'000, 3'000'000}, 600'000, 300'000, 1, 0) == soft);
    CHECK_FALSE(obs.segment_ok({7'000'000, 4'000'000}, {8'000'000, 4'000'000}, 1, 200'000, 2));
    if (soft) CHECK(obs.via_ok({7'000'000, 4'000'000}, 600'000, 300'000, 2, 0));
    CHECK_FALSE(obs.via_ok({10'000'000, 5'000'000}, 600'000, 300'000, 2, 0));
    CHECK_FALSE(obs.via_ok({4'000'000, 5'000'000}, 600'000, 300'000, 2, 0));  // same-net pad: physical rule remains hard
    CHECK_FALSE(obs.via_ok({0, 5'000'000}, 600'000, 300'000, 2, 0));
  }
}

TEST_CASE("soft zones target unused planes with legal vias but not already connected fills", "[route][soft-zones]") {
  model::DesignRules rules;
  rules.classes.emplace_back();
  route::RouterOptions o;
  o.work_budget = 200'000;
  o.gpu_device = -1;
  o.optimize = false;
  const auto b = parse(board_text(plane("In1.Cu", 2, "GND", kPts) + kArea));
  CHECK(route::Router(b, rules, o).run().connections == 0);
  o.soft_zones = true;
  const auto r = route::Router(b, rules, o).run();
  REQUIRE(r.connections == 1);
  CHECK(r.routed == 1);
  REQUIRE_FALSE(r.vias.empty());
  auto working = b;
  route::Obstacles obs(working, rules, true);
  for (const auto& v : r.vias) CHECK(obs.via_ok(v.pos, v.size, v.drill, v.net, 0));
  const auto joined = parse(board_text(plane("F.Cu", 2, "GND", kPts)));
  CHECK(route::Router(joined, rules, o).run().connections == 0);
}

TEST_CASE("unreachable soft planes do not replace same-layer pad connections", "[route][soft-zones][soft-zone-regression]") {
  auto b = parse(board_text(plane("In1.Cu", 1, "SIG", kPts)));
  b.pads[0].net = 1;
  b.pads.push_back(b.pads[0]);
  b.pads.back().pos = {8'000'000, 5'000'000};
  model::DesignRules rules;
  rules.classes.emplace_back();
  route::RouterOptions o;
  o.pitch = 100'000;
  o.work_budget = 200'000;
  o.gpu_device = -1;
  o.optimize = false;
  SECTION("router disables all layer changes") {
    o.allow_vias = false;
    o.blind_vias = false;
  }
  SECTION("the net disallows vias, including blind vias") {
    o.blind_vias = true;
    rules.minimums.allow_blind_buried_vias = true;
    model::CustomRule rule;
    rule.name = "no SIG vias";
    rule.condition = "A.NetName == 'SIG'";
    model::Constraint constraint;
    constraint.type = "disallow";
    constraint.items = {"via"};
    rule.constraints.push_back(constraint);
    rules.custom.push_back(rule);
  }
  SECTION("the net may not route on the plane layer") {
    model::CustomRule rule;
    rule.name = "no SIG inner tracks";
    rule.condition = "A.NetName == 'SIG'";
    rule.layer = "inner";
    model::Constraint constraint;
    constraint.type = "disallow";
    constraint.items = {"track"};
    rule.constraints.push_back(constraint);
    rules.custom.push_back(rule);
  }
  const auto hard = route::Router(b, rules, o).run();
  REQUIRE(hard.connections == 1);
  REQUIRE(hard.routed == hard.connections);
  REQUIRE(hard.failures.empty());
  o.soft_zones = true;
  const auto soft = route::Router(b, rules, o).run();
  CHECK(soft.connections == hard.connections);
  CHECK(soft.routed == soft.connections);
  CHECK(soft.failures.empty());
  CHECK(soft.vias.empty());
}

TEST_CASE("one through via joins all aligned same-net soft planes", "[route][soft-zones][soft-zone-regression]") {
  std::string second = kPts;
  SECTION("both fills contain the via centre") {}
  SECTION("the via annulus overlaps the second fill outside its boundary") {
    second = "(xy 1 1) (xy 3.9 1) (xy 3.9 9) (xy 1 9)";
  }
  const auto b = parse(board_text(plane("In1.Cu", 2, "GND", kPts) + plane("In2.Cu", 2, "GND", second.c_str())));
  model::DesignRules rules;
  rules.classes.emplace_back();
  route::RouterOptions o;
  o.pitch = 100'000;
  o.work_budget = 200'000;
  o.gpu_device = -1;
  o.optimize = false;
  o.soft_zones = true;
  const auto r = route::Router(b, rules, o).run();
  REQUIRE(r.connections == 2);
  CHECK(r.routed == r.connections);
  CHECK(r.failures.empty());
  REQUIRE(r.vias.size() == 1);
  CHECK(r.vias[0].type == model::ViaType::Through);
  CHECK(r.vias[0].layer_top == 0);
  CHECK(r.vias[0].layer_bottom == b.copper_count() - 1);
  auto routed = b;
  routed.tracks.insert(routed.tracks.end(), r.tracks.begin(), r.tracks.end());
  routed.vias.insert(routed.vias.end(), r.vias.begin(), r.vias.end());
  route::Obstacles obs(routed, rules, true);
  const auto connected = drc::compute_connectivity(routed, obs.copper(), obs.grid());
  REQUIRE_FALSE(connected.root.empty());
  CHECK(std::all_of(connected.root.begin(), connected.root.end(), [&](int root) { return root == connected.root.front(); }));
}

TEST_CASE("teardrop zones stay fixed copper and never become plane targets", "[route][soft-zones]") {
  // A teardrop left behind when routing is deleted, away from the pad (KiCad marks them (attr (teardrop ...))).
  const auto pour = plane("In1.Cu", 2, "GND", "(xy 7 4) (xy 9 4) (xy 9 6) (xy 7 6)");
  auto teardrop = pour;
  teardrop.insert(teardrop.find("(hatch"), "(attr (teardrop (type padvia))) ");
  const auto b_pour = parse(board_text(pour)), b_drop = parse(board_text(teardrop));
  CHECK_FALSE(b_pour.zones[0].teardrop);
  REQUIRE(b_drop.zones[0].teardrop);
  model::DesignRules rules;
  rules.classes.emplace_back();
  route::RouterOptions o;
  o.work_budget = 200'000;
  o.gpu_device = -1;
  o.optimize = false;
  o.soft_zones = true;
  CHECK(route::Router(b_pour, rules, o).run().connections == 1);
  CHECK(route::Router(b_drop, rules, o).run().connections == 0);
  auto wp = b_pour, wd = b_drop;
  route::Obstacles soft_pour(wp, rules, true), soft_drop(wd, rules, true);
  const geom::Point a{6'000'000, 5'000'000}, c{10'000'000, 5'000'000};
  CHECK(soft_pour.segment_ok(a, c, 1, 200'000, 1));
  CHECK_FALSE(soft_drop.segment_ok(a, c, 1, 200'000, 1));
  for (int x = 12; x <= 20; ++x)
    for (int y = 6; y <= 14; ++y) {
      const geom::Point p{x * 500'000, y * 500'000};
      CHECK(soft_drop.fixed_via_code(p, 600'000, 300'000, 0, 1) == soft_drop.fixed_via_code_reference(p, 600'000, 300'000, 0, 1));
    }
}

TEST_CASE("plane target box and layer heuristic preserves zero-heuristic costs", "[route][soft-zones]") {
  model::DesignRules rules;
  rules.classes.emplace_back();
  route::RouterOptions o;
  o.pitch = 50'000;
  o.work_budget = 2'000'000;
  o.gpu_device = -1;
  o.field_min_cells = 1; // plane targets must still bypass cost-to-go fields
  o.soft_zones = true;
  o.plane_cut_cost = 0;
  CHECK(route::RouterOptions{}.plane_cut_cost == 0);
  o.optimize = false;
  for (bool beside : {false, true}) {
    auto b = parse(board_text(plane("In1.Cu", 2, "GND",
        beside ? "(xy 8 1) (xy 11 1) (xy 11 9) (xy 8 9)" : kPts)));
    // One lattice-aligned start and a straight route make the exact search cost observable in the
    // emitted copper: no stubs, bends, foreign planes, history, soft obstacles or clean-up costs.
    b.pads[0].size_x = b.pads[0].size_y = 10'000;
    o.zone_target_heuristic = false;
    const auto reference = route::Router(b, rules, o).run();
    o.zone_target_heuristic = true;
    const auto bounded = route::Router(b, rules, o).run();
    INFO("beside " << beside << ", zero heuristic " << reference.expansions << ", bounded " << bounded.expansions);
    REQUIRE(reference.routed == 1);
    REQUIRE(bounded.routed == 1);
    REQUIRE(reference.vias.size() == 1);
    REQUIRE(bounded.vias.size() == 1);
    const auto cost = [&](const route::RouteResult& r) {
      Coord result = static_cast<Coord>(o.via_cost_mm * 1e6);
      for (const auto& t : r.tracks) {
        REQUIRE(t.a.y == t.b.y);
        REQUIRE(t.b.x > t.a.x);
        REQUIRE((t.b.x - t.a.x) % o.pitch == 0);
        result += t.b.x - t.a.x;
      }
      return result;
    };
    CHECK(cost(bounded) == cost(reference));
    // The displaced target also requires an initial unsuccessful window: count that work in the
    // bound rather than claiming only the successful search's speed-up.
    CHECK(bounded.expansions * (beside ? 2 : 5) < reference.expansions);
    if (!beside) CHECK(bounded.expansions <= 2);
  }
}

TEST_CASE("plane map matches its reference with priorities and holes, without class-dependent ownership", "[route][soft-zones]") {
  auto b = parse(board_text(plane("In1.Cu", 2, "GND", kPts) + plane("In1.Cu", 1, "SIG", "(xy 2 2) (xy 8 2) (xy 8 8) (xy 2 8)") + kArea));
  b.zones[1].priority = 1;
  b.zones[1].outline.push_back({{3'000'000, 3'000'000}, {5'000'000, 3'000'000}, {5'000'000, 6'000'000}, {3'000'000, 6'000'000}});
  route::PlaneMap map;
  REQUIRE(map.build(b, {0, 0}, 250'000, 49, 41));
  for (int l = 0; l < 4; ++l)
    for (int y = 0; y < 41; ++y)
      for (int x = 0; x < 49; ++x)
        CHECK(map.net_at(l, x, y) == route::PlaneMap::reference(b, {x * 250'000, y * 250'000}, l));
  CHECK(map.cost(1, 24, 20, 2, 500'000) == 500'000);
  CHECK(map.cost(1, 24, 20, 1, 500'000) == 0);
  CHECK(map.cost(1, 16, 20, 2, 500'000) == 0);
  CHECK(map.cost(1, 24, 20, 2, 0) == 0);
}

TEST_CASE("plane scanlines preserve concave lattice-aligned boundaries and holes", "[route][soft-zones]") {
  const auto outline = "(xy 1 1) (xy 11 1) (xy 11 9) (xy 8 9) (xy 8 5) (xy 6 5) (xy 6 9) (xy 1 9)";
  auto b = parse(board_text(plane("In1.Cu", 2, "GND", kPts) + plane("In1.Cu", 1, "SIG", outline)));
  b.zones[1].priority = 2;
  b.zones[1].outline.push_back({{2'000'000, 3'000'000}, {4'000'000, 3'000'000}, {4'000'000, 7'000'000}, {2'000'000, 7'000'000}});
  // A diagonal outer boundary also crosses exact lattice points; reversed rings retain the same fill.
  b.zones.push_back(b.zones[1]);
  b.zones.back().copper = model::layer_bit(2);
  b.zones.back().outline = {{{1'000'000, 1'000'000}, {11'000'000, 9'000'000}, {1'000'000, 9'000'000}},
                          {{2'000'000, 5'000'000}, {4'000'000, 7'000'000}, {2'000'000, 7'000'000}}};
  for (bool reverse : {false, true}) {
    if (reverse) {
      for (auto& zone : b.zones) {
        for (auto& ring : zone.outline) std::reverse(ring.begin(), ring.end());
      }
    }
    route::PlaneMap map;
    const geom::Point origin{-500'000, -500'000};
    REQUIRE(map.build(b, origin, 250'000, 53, 45));
    for (int l = 0; l < 4; ++l) {
      for (int y = 0; y < 45; ++y) {
        for (int x = 0; x < 53; ++x) {
          const geom::Point point{origin.x + x * 250'000, origin.y + y * 250'000};
          CHECK(map.net_at(l, x, y) == route::PlaneMap::reference(b, point, l));
        }
      }
    }
    CHECK(map.net_at(1, 6, 6) == 1);   // outer vertex is inside
    CHECK(map.net_at(1, 10, 14) == 2); // hole vertex falls back to the lower-priority zone
    CHECK(map.net_at(1, 26, 26) == 1); // notch boundary is inside
    CHECK(map.net_at(1, 30, 30) == 2); // notch interior is outside
  }
}

TEST_CASE("plane map supports uint16 zone indices and falls back above its capacity", "[route][soft-zones]") {
  auto b = parse(board_text(plane("In1.Cu", 2, "GND", kPts)));
  const auto zone = b.zones.front();
  b.zones.assign(65535, zone);
  b.zones.back().net = 123456; // the table index, not the NetId, must fit uint16
  b.zones.back().priority = 1;
  route::PlaneMap map;
  REQUIRE(map.build(b, {4'000'000, 5'000'000}, 250'000, 1, 1));
  CHECK(map.net_at(1, 0, 0) == 123456);
  b.zones.push_back(zone);
  CHECK_FALSE(map.build(b, {4'000'000, 5'000'000}, 250'000, 1, 1));
  CHECK(map.net_at(1, 0, 0) == 0);
  CHECK(map.cost(1, 0, 0, 1, 500'000) == 0);
  b.zones.clear();
  REQUIRE(map.build(b, {4'000'000, 5'000'000}, 250'000, 1, 1));
  CHECK(map.net_at(1, 0, 0) == 0);
}

TEST_CASE("plane penalty selects the equal-length route outside foreign copper", "[route][soft-zones]") {
  const auto foreign = plane("F.Cu", 2, "GND", "(xy 4 1) (xy 8 1) (xy 8 4.9) (xy 4 4.9)");
  auto b = parse(board_text(foreign));
  b.pads[0].net = 1;
  b.pads[0].pos = {2'000'000, 5'000'000};
  b.pads.push_back(b.pads[0]);
  b.pads.back().pos = {10'000'000, 5'000'000};
  b.pads.push_back(b.pads[0]);
  b.pads.back().net = 0;
  b.pads.back().pos = {6'000'000, 5'000'000};
  b.pads.back().size_x = b.pads.back().size_y = 2'000'000;
  model::DesignRules rules;
  rules.classes.emplace_back();
  route::RouterOptions o;
  o.pitch = 100'000;
  o.work_budget = 500'000;
  o.gpu_device = -1;
  o.soft_zones = true;
  o.optimize = false;
  o.allow_vias = false;
  o.plane_cut_cost = 0;
  const auto zero = route::Router(b, rules, o).run();
  REQUIRE(zero.routed == 1);
  Coord side = 0;
  for (const auto& t : zero.tracks) side += t.a.y + t.b.y - 10'000'000;
  REQUIRE(side != 0);
  // Cover the zero-penalty winner, then require the search to choose its equally long mirrored alternative.
  if (side > 0) {
    for (auto& p : b.zones[0].outline[0]) p.y = 10'000'000 - p.y;
    b.zones[0].fills[0].second = b.zones[0].outline[0];
  }
  const auto shape = geom::Shape::polygon(b.zones[0].outline[0]);
  REQUIRE(std::any_of(zero.tracks.begin(), zero.tracks.end(), [&](const auto& t) {
    return geom::closer_than(geom::Shape::segment(t.a, t.b, t.width / 2), shape, 0);
  }));
  o.plane_cut_cost = 2;
  const auto r = route::Router(b, rules, o).run();
  REQUIRE(r.routed == 1);
  for (const auto& t : r.tracks) CHECK_FALSE(geom::closer_than(geom::Shape::segment(t.a, t.b, t.width / 2), shape, 0));
}

TEST_CASE("soft zone writer strips only invalidated fills and preserves untouched zone bytes", "[route][soft-zones][io]") {
  const auto cut = plane("In1.Cu", 2, "GND", kPts);
  const auto untouched = plane("In2.Cu", 1, "SIG", kPts);
  const auto text = board_text(cut + untouched);
  io::LoadedBoard lb{sexpr::Document::parse(text), {}};
  lb.board = io::read_board(lb.doc);
  io::BoardEditor ed(lb);
  CHECK(ed.write() == text);
  model::Track t;
  t.a = {2'000'000, 3'000'000};
  t.b = {8'000'000, 3'000'000};
  t.width = 200'000;
  t.layer = 1;
  t.net = 2;
  CHECK(ed.invalidate_zone_fills({t}, {}) == 0);  // same-net copper does not cut a plane
  CHECK(ed.write() == text);
  t.net = 1;
  CHECK(ed.invalidate_zone_fills({t}, {}) == 1);
  const auto out = ed.write();
  CHECK(out.find(untouched) != std::string::npos);
  const auto read = parse(out);
  REQUIRE(read.zones.size() == 2);
  CHECK(read.zones[0].fills.empty());
  CHECK(read.zones[1].fills == lb.board.zones[1].fills);

  io::LoadedBoard via_lb{sexpr::Document::parse(text), {}};
  via_lb.board = io::read_board(via_lb.doc);
  model::Via v;
  v.pos = {3'000'000, 3'000'000};
  v.size = 600'000;
  v.drill = 300'000;
  v.net = 2;
  v.layer_top = 0;
  v.layer_bottom = 3;
  CHECK(io::BoardEditor(via_lb).invalidate_zone_fills({}, {v}) == 1);
  CHECK(parse(via_lb.doc.write()).zones[0].fills == via_lb.board.zones[0].fills);
  CHECK(parse(via_lb.doc.write()).zones[1].fills.empty());
}

TEST_CASE("plane cost scales with route length without flooding a board-wide pour", "[route][soft-zones]") {
  const auto text = std::string("(kicad_pcb (version 20240108) (generator \"pcbnew\")"
      " (layers (0 \"F.Cu\" signal) (31 \"B.Cu\" signal) (44 \"Edge.Cuts\" user))"
      " (net 0 \"\") (net 1 \"SIG\") (net 2 \"GND\")"
      " (footprint \"R\" (layer \"F.Cu\") (at 3 6) (property \"Reference\" \"R1\" (at 0 0) (layer \"F.SilkS\"))"
      " (pad \"1\" smd rect (at 0 0) (size 0.1 0.1) (layers \"F.Cu\") (net 1 \"SIG\")))"
      " (footprint \"R\" (layer \"F.Cu\") (at 31 6) (property \"Reference\" \"R2\" (at 0 0) (layer \"F.SilkS\"))"
      " (pad \"1\" smd rect (at 0 0) (size 0.1 0.1) (layers \"F.Cu\") (net 1 \"SIG\")))"
      " (footprint \"H\" (layer \"F.Cu\") (at 17 6)"
      " (pad \"\" np_thru_hole circle (at 0 0) (size 6 6) (drill 6) (layers \"*.Cu\" \"*.Mask\")))"
      " (gr_rect (start 0 0) (end 34 12) (layer \"Edge.Cuts\") (stroke (width 0.1) (type solid))) ") +
      plane("F.Cu", 2, "GND", "(xy 0 0) (xy 34 0) (xy 34 12) (xy 0 12)") + ")";
  const auto b = parse(text);
  model::DesignRules rules;
  rules.classes.emplace_back();
  route::RouterOptions o;
  o.pitch = 100'000;
  o.work_budget = 500'000;
  o.gpu_device = -1;
  o.field_heuristic = false;
  o.soft_zones = true;
  o.optimize = false;
  o.allow_vias = false;
  o.plane_cut_cost = 0;
  const auto plain = route::Router(b, rules, o).run();
  REQUIRE(plain.routed == 1);
  o.plane_cut_cost = 0.5;
  const auto penalized = route::Router(b, rules, o).run();
  REQUIRE(penalized.routed == 1);
  INFO("plain expansions " << plain.expansions << ", penalized expansions " << penalized.expansions);
  // The old 0.5 mm per-cell charge at this 0.1 mm pitch made orthogonal steps cost 6x, not 1.5x.
  // The heuristic deliberately ignores planes; cap its added work rather than changing the lower bound.
  CHECK(penalized.expansions <= 5 * plain.expansions);

  const Coord diag = static_cast<Coord>(std::llround(static_cast<double>(o.pitch) * std::numbers::sqrt2));
  CHECK(route::PlaneMap::scaled_penalty(o.pitch, 0.5) == 50'000);
  CHECK(route::PlaneMap::scaled_penalty(diag, 0.5) == 70'710);
  CHECK(route::PlaneMap::scaled_penalty(3'000'000, 0.5) == 1'500'000); // per foreign layer crossed by a via
  CHECK(route::PlaneMap::scaled_penalty(diag, 0) == 0);
  auto cost = [&](const route::RouteResult& r, double factor) {
    std::int64_t result = 0;
    geom::Point previous;
    for (const auto& t : r.tracks) {
      const Coord dx = std::llabs(t.b.x - t.a.x), dy = std::llabs(t.b.y - t.a.y);
      REQUIRE((dx == 0 || dy == 0 || dx == dy));
      REQUIRE(std::max(dx, dy) % o.pitch == 0);
      CHECK(geom::point_in_polygon(t.a, b.zones[0].outline[0]));
      CHECK(geom::point_in_polygon(t.b, b.zones[0].outline[0]));
      const auto steps = std::max(dx, dy) / o.pitch;
      const auto length = dx && dy ? diag : o.pitch;
      result += steps * (length + route::PlaneMap::scaled_penalty(length, factor));
      const geom::Point direction{(t.b.x > t.a.x) - (t.b.x < t.a.x), (t.b.y > t.a.y) - (t.b.y < t.a.y)};
      if (!(previous == geom::Point{}) && !(direction == previous)) {
        const auto dot = previous.x * direction.x + previous.y * direction.y;
        REQUIRE(dot >= 0);
        result += dot > 0 ? o.pitch / 2 : 2 * o.pitch;
      }
      previous = direction;
    }
    return result;
  };
  // This sums the same integer step penalties as A*: a board-wide pour cannot charge more than F times
  // the geometric path cost. Bend costs are unchanged, so the total retains the same upper bound.
  CHECK(2 * cost(penalized, o.plane_cut_cost) <= 3 * cost(plain, 0));
}
