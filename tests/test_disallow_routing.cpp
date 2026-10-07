// SPDX-License-Identifier: GPL-3.0-or-later
// Candidate legality uses the same unary rule evaluator as DRC; search disks remain optimistic.
#include <catch2/catch_test_macros.hpp>

#include <string>
#include <utility>

#include "drc/drc.hpp"
#include "io/kicad/board_reader.hpp"
#include "route/obstacles.hpp"
#include "route/router.hpp"

using namespace tmk;

namespace {
model::Board disallow_board(const std::string& extra = "") {
  return io::read_board(sexpr::Document::parse(
      "(kicad_pcb (version 20240108) (generator pcbnew) "
      "(layers (0 F.Cu signal) (1 In1.Cu signal) (2 In2.Cu signal) (31 B.Cu signal) "
      "(44 Edge.Cuts user) (46 B.CrtYd user) (47 F.CrtYd user)) "
      "(net 0 \"\") (net 1 SIG) (net 2 GND) "
      "(gr_rect (start 0 0) (end 20 10) (layer Edge.Cuts) (stroke (width 0.1) (type solid))) "
      "(zone (net 0) (net_name \"\") (layers F.Cu In1.Cu In2.Cu B.Cu) (name box) "
      "(hatch edge 0.5) (connect_pads (clearance 0)) (min_thickness 0.25) "
      "(keepout (tracks allowed) (vias allowed) (pads allowed) (copperpour allowed) (footprints allowed)) "
      "(polygon (pts (xy 8 3) (xy 12 3) (xy 12 7) (xy 8 7)))) " + extra + ")"));
}

model::DesignRules disallow_rules(std::string condition, std::string item = "track", std::string layer = "") {
  model::DesignRules rules;
  rules.classes.push_back(model::NetClass{});
  rules.classes[0].name = "Default";
  model::CustomRule rule;
  rule.name = "candidate prohibition";
  rule.condition = std::move(condition);
  rule.layer = std::move(layer);
  rule.constraints.push_back({"disallow", {}, {}, {}, {std::move(item)}});
  rules.custom.push_back(std::move(rule));
  return rules;
}

void check_via_codes(route::Obstacles& obs, geom::Point p, Coord diameter, Coord margin, model::NetId net, bool blocked) {
  const auto expected = blocked ? route::Obstacles::kBlocked : route::Obstacles::kFree;
  CHECK(obs.fixed_via_code(p, diameter, 200'000, margin, net) == expected);
  CHECK(obs.fixed_via_code_reference(p, diameter, 200'000, margin, net) == expected);
  CHECK(obs.via_state(p, diameter, 200'000, net, margin, false) == (blocked ? 2 : 0));
}
}  // namespace

TEST_CASE("positional track disallow uses actual width, net and layer on every legality path", "[rules][route][disallow]") {
  auto board = disallow_board();
  const auto rules = disallow_rules("A.NetName != 'GND' && A.Width >= 0.3mm && A.intersectsArea('box')", "track", "F.Cu");
  route::Obstacles obs(board, rules);
  CHECK(obs.needs_exact_routing());
  const geom::Point p{10'000'000, 5'000'000};
  CHECK(obs.disk_state(p, 0, 150'000, 1, 0, false) == 2);
  CHECK(obs.fixed_code(p, 0, 150'000, 0, 1) == route::Obstacles::kBlocked);
  CHECK(obs.disk_state(p, 0, 100'000, 1, 0, false) == 0);
  CHECK(obs.disk_state(p, 0, 150'000, 2, 0, false) == 0);
  CHECK(obs.fixed_code(p, 0, 150'000, 0, 2) == route::Obstacles::kFree);
  CHECK(obs.segment_state({6'000'000, 5'000'000}, {14'000'000, 5'000'000}, 0, 300'000, 1, false) == 2);
  CHECK(obs.segment_state({6'000'000, 5'000'000}, {14'000'000, 5'000'000}, 0, 200'000, 1, false) == 0);
  CHECK(obs.segment_state({6'000'000, 5'000'000}, {14'000'000, 5'000'000}, 1, 300'000, 1, false) == 0);
  CHECK(obs.via_state(p, 600'000, 200'000, 1, 0, false) == 0);  // not a track surrogate
}

TEST_CASE("search margins are not copper geometry for disallow predicates", "[rules][route][disallow]") {
  auto board = disallow_board();
  const geom::Point p{7'600'000, 5'000'000};
  {
    const auto rules = disallow_rules("A.intersectsArea('box')");
    route::Obstacles obs(board, rules);
    CHECK(obs.disk_state(p, 0, 100'000, 1, 500'000, false) == 0);
    CHECK(obs.fixed_code(p, 0, 100'000, 500'000, 1) == route::Obstacles::kFree);
  }
  {
    const auto rules = disallow_rules("A.intersectsArea('box')", "via");
    route::Obstacles obs(board, rules);
    check_via_codes(obs, p, 200'000, 500'000, 1, false);
    check_via_codes(obs, {8'050'000, 5'000'000}, 200'000, 0, 1, true);
  }
}

TEST_CASE("enclosure disallow permits partial crossing and search disks do not overblock it", "[rules][route][disallow]") {
  auto board = disallow_board();
  const auto rules = disallow_rules("A.enclosedByArea('box')");
  route::Obstacles obs(board, rules);
  CHECK(obs.disk_state({10'000'000, 5'000'000}, 0, 100'000, 1, 0, false) == 0);
  CHECK(obs.fixed_code({10'000'000, 5'000'000}, 0, 100'000, 0, 1) == route::Obstacles::kFree);
  CHECK(obs.segment_state({9'000'000, 5'000'000}, {11'000'000, 5'000'000}, 0, 200'000, 1, false) == 2);
  CHECK(obs.segment_state({6'000'000, 5'000'000}, {14'000'000, 5'000'000}, 0, 200'000, 1, false) == 0);
  CHECK(obs.segment_state({10'000'000, 5'000'000}, {14'000'000, 5'000'000}, 0, 200'000, 1, false) == 0);
}

TEST_CASE("negated area disallow keeps track search optimistic", "[rules][route][disallow]") {
  auto board = disallow_board();
  const auto rules = disallow_rules("!A.intersectsArea('box')");
  route::Obstacles obs(board, rules);
  CHECK(obs.disk_state({6'000'000, 5'000'000}, 0, 100'000, 1, 0, false) == 0);
  CHECK(obs.fixed_code({6'000'000, 5'000'000}, 0, 100'000, 0, 1) == route::Obstacles::kFree);
  CHECK(obs.segment_state({5'000'000, 5'000'000}, {6'000'000, 5'000'000}, 0, 200'000, 1, false) == 2);
  CHECK(obs.segment_state({6'000'000, 5'000'000}, {14'000'000, 5'000'000}, 0, 200'000, 1, false) == 0);
}

TEST_CASE("ignored spatial exception cannot be rejected by a geometry-less mask or track sample", "[rules][route][disallow]") {
  auto board = disallow_board();
  auto rules = disallow_rules("A.NetName == 'SIG'");
  auto exemption = rules.custom.front();
  exemption.name = "crossing exception";
  exemption.condition = "A.intersectsArea('box')";
  exemption.severity = "ignore";
  rules.custom.push_back(exemption);
  route::Obstacles obs(board, rules);
  CHECK(obs.needs_exact_routing());
  CHECK(obs.disk_state({6'000'000, 5'000'000}, 0, 100'000, 1, 0, false) == 0);
  CHECK(obs.segment_state({6'000'000, 5'000'000}, {14'000'000, 5'000'000}, 0, 200'000, 1, false) == 0);
  CHECK(obs.segment_state({5'000'000, 5'000'000}, {6'000'000, 5'000'000}, 0, 200'000, 1, false) == 2);
}

TEST_CASE("coordinate predicates use final track anchors and actual via anchors", "[rules][route][disallow]") {
  auto board = disallow_board();
  const auto rules = disallow_rules("A.Position_X >= 10mm");
  route::Obstacles obs(board, rules);
  CHECK(obs.disk_state({11'000'000, 5'000'000}, 0, 100'000, 1, 0, false) == 0);
  CHECK(obs.segment_state({9'000'000, 5'000'000}, {11'000'000, 5'000'000}, 0, 200'000, 1, false) == 0);
  CHECK(obs.segment_state({11'000'000, 5'000'000}, {9'000'000, 5'000'000}, 0, 200'000, 1, false) == 2);
  const auto via_rules = disallow_rules("A.Position_X >= 10mm", "via");
  route::Obstacles via_obs(board, via_rules);
  check_via_codes(via_obs, {11'000'000, 5'000'000}, 600'000, 0, 1, true);
  check_via_codes(via_obs, {9'000'000, 5'000'000}, 600'000, 0, 1, false);
}

TEST_CASE("via disallow uses actual subtype, span and item layer", "[rules][route][disallow]") {
  auto board = disallow_board();
  const geom::Point p{10'000'000, 5'000'000};
  for (const auto& item : {std::string("through_via"), std::string("blind_via"), std::string("buried_via"), std::string("micro_via")}) {
    INFO(item);
    const auto rules = disallow_rules("A.intersectsArea('box')", item);
    route::Obstacles obs(board, rules);
    check_via_codes(obs, p, 600'000, 0, 1, item == "through_via");
    CHECK(obs.via_state_span(p, 600'000, 200'000, 1, 0, false, nullptr, 0, 1) == (item == "blind_via" ? 2 : 0));
    CHECK(obs.via_state_span(p, 600'000, 200'000, 1, 0, false, nullptr, 1, 2) == (item == "buried_via" ? 2 : 0));
    CHECK(obs.via_state_span(p, 300'000, 100'000, 1, 0, false, nullptr, 0, 1, model::ViaType::Micro) ==
          (item == "micro_via" ? 2 : 0));
  }
  {
    const auto rules = disallow_rules("A.existsOnLayer('B.Cu')", "via");
    route::Obstacles obs(board, rules);
    check_via_codes(obs, p, 600'000, 0, 1, true);
    CHECK(obs.via_state_span(p, 600'000, 200'000, 1, 0, false, nullptr, 0, 1) == 0);
    CHECK(obs.via_state_span(p, 600'000, 200'000, 1, 0, false, nullptr, 2, 3) == 2);
  }
  {
    const auto rules = disallow_rules("A.Layer == 'In1.Cu'", "via");
    route::Obstacles obs(board, rules);
    check_via_codes(obs, p, 600'000, 0, 1, false);
    CHECK(obs.via_state_span(p, 600'000, 200'000, 1, 0, false, nullptr, 0, 1) == 0);
    CHECK(obs.via_state_span(p, 600'000, 200'000, 1, 0, false, nullptr, 1, 2) == 2);
  }
}

TEST_CASE("fixed via codes agree with the evaluator across candidate nets widths and margins", "[rules][route][disallow]") {
  auto board = disallow_board();
  const auto rules = disallow_rules("A.NetName != 'GND' && A.Width >= 0.4mm && A.intersectsArea('box')", "via");
  route::Obstacles obs(board, rules);
  for (Coord x = 7'000'000; x <= 13'000'000; x += 125'000)
    for (Coord diameter : {200'000, 600'000})
      for (Coord margin : {0, 250'000})
        for (model::NetId net : {1, 2}) {
          drc::CopperItem candidate;
          candidate.kind = drc::ItemKind::Via;
          candidate.net = net;
          candidate.layers = 15;
          candidate.anchor_layer = 0;
          candidate.pos = {x, 5'000'000};
          candidate.width = diameter;
          candidate.shapes = {geom::Shape::point(candidate.pos, diameter / 2)};
          candidate.box = candidate.shapes.front().box;
          const bool blocked = !obs.rules().candidate_allowed(candidate, 0);
          check_via_codes(obs, candidate.pos, diameter, margin, net, blocked);
        }
}

TEST_CASE("router commits no disallowed positional tracks after search and segment merging", "[rules][route][disallow]") {
  auto board = disallow_board(
      "(footprint R (layer F.Cu) (at 4 5) (property Reference R1 (at 0 0) (layer F.SilkS)) "
      "(pad 1 smd rect (at 0 0) (size 1 1) (layers F.Cu) (net 1 SIG))) "
      "(footprint R (layer F.Cu) (at 16 5) (property Reference R2 (at 0 0) (layer F.SilkS)) "
      "(pad 1 smd rect (at 0 0) (size 1 1) (layers F.Cu) (net 1 SIG)))");
  const auto rules = disallow_rules("A.intersectsArea('box')");
  route::RouterOptions options;
  options.work_budget = 2'000'000;
  options.gpu_device = -1;
  options.allow_vias = false;
  const auto result = route::Router(board, rules, options).run();
  REQUIRE(result.routed == 1);
  REQUIRE_FALSE(result.tracks.empty());
  board.tracks.insert(board.tracks.end(), result.tracks.begin(), result.tracks.end());
  const auto report = drc::run_drc(board, rules);
  for (const auto& violation : report.violations) CHECK(violation.type != "items_not_allowed");
}

TEST_CASE("courtyard disallow is applied to actual routed geometry rather than ownership", "[rules][route][disallow]") {
  auto board = disallow_board(
      "(footprint IC (layer F.Cu) (at 10 5) (property Reference U1 (at 0 0) (layer F.SilkS)) "
      "(fp_rect (start -2 -2) (end 2 2) (layer F.CrtYd) (stroke (width 0.05) (type solid)) (fill none)))");
  for (const auto& function : {"intersectsCourtyard", "insideCourtyard"}) {
    INFO(function);
    const auto rules = disallow_rules(std::string("A.") + function + "('U1')", "via");
    route::Obstacles obs(board, rules);
    check_via_codes(obs, {10'000'000, 5'000'000}, 600'000, 0, 1, true);
    check_via_codes(obs, {6'000'000, 5'000'000}, 600'000, 0, 1, false);
  }
  const auto member_rules = disallow_rules("A.memberOfFootprint('U1')", "via");
  route::Obstacles member_obs(board, member_rules);
  check_via_codes(member_obs, {10'000'000, 5'000'000}, 600'000, 0, 1, false);
  const auto track_rules = disallow_rules("A.intersectsCourtyard('U1')");
  route::Obstacles track_obs(board, track_rules);
  CHECK(track_obs.segment_state({6'000'000, 5'000'000}, {14'000'000, 5'000'000}, 0, 200'000, 1, false) == 2);
}

TEST_CASE("via enclosure uses actual diameter without search inflation", "[rules][route][disallow]") {
  auto board = disallow_board();
  const auto rules = disallow_rules("A.enclosedByArea('box')", "via");
  route::Obstacles obs(board, rules);
  check_via_codes(obs, {10'000'000, 5'000'000}, 600'000, 0, 1, true);
  check_via_codes(obs, {8'050'000, 5'000'000}, 600'000, 0, 1, false);
  check_via_codes(obs, {8'400'000, 5'000'000}, 600'000, 500'000, 1, true);
}

TEST_CASE("fixed via reference preserves odd nanometre candidate diameters", "[rules][route][disallow]") {
  auto board = disallow_board();
  const auto rules = disallow_rules("A.Width > 0.6mm", "via");
  route::Obstacles obs(board, rules);
  check_via_codes(obs, {10'000'000, 5'000'000}, 600'000, 0, 1, false);
  check_via_codes(obs, {10'000'000, 5'000'000}, 600'001, 0, 1, true);
}
