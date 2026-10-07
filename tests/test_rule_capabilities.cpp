// SPDX-License-Identifier: GPL-3.0-or-later
#include <catch2/catch_test_macros.hpp>

#include <algorithm>
#include <string>

#include "drc/drc.hpp"
#include "io/kicad/board_reader.hpp"
#include "route/obstacles.hpp"

using namespace tmk;
namespace {
model::Board capability_board() {
  return io::read_board(sexpr::Document::parse(
      "(kicad_pcb (version 20240108) (generator pcbnew) "
      "(layers (0 F.Cu signal) (31 B.Cu signal) (44 Edge.Cuts user)) "
      "(net 0 \"\") (net 1 SIG) "
      "(gr_rect (start 0 0) (end 20 10) (layer Edge.Cuts) (stroke (width 0.1) (type solid))) "
      "(segment (start 4 5) (end 16 5) (width 0.25) (layer F.Cu) (net 1)))"));
}
model::DesignRules capability_rules() {
  model::DesignRules rules;
  rules.classes.emplace_back();
  rules.classes.front().name = "Default";
  return rules;
}
model::CustomRule prohibition(std::string condition, std::string severity = "") {
  model::CustomRule rule;
  rule.name = "named prohibition";
  rule.condition = std::move(condition);
  rule.severity = std::move(severity);
  rule.constraints.push_back({"disallow", {}, {}, {}, {"track", "via"}});
  return rule;
}
bool warns(const drc::RuleEngine& engine, const std::string& symbol) {
  return std::any_of(engine.warnings().begin(), engine.warnings().end(), [&](const auto& warning) {
    return warning.find(symbol) != std::string::npos;
  });
}
}

TEST_CASE("unknown rule symbols are diagnosed structurally, even in hidden branches", "[rules][disallow]") {
  auto board = capability_board();
  for (const std::string expression : {"false && A.Missing_Property == 1", "true || B.missingFunction('x')",
                                       "A.isPlated('invalid argument')", "A.L == 'F.Cu'", "NetName == 'SIG'"}) {
    auto rules = capability_rules();
    rules.custom.push_back(prohibition(expression));
    drc::RuleEngine engine(board, rules);
    CAPTURE(expression);
    REQUIRE_FALSE(engine.warnings().empty());
    CHECK(engine.needs_exact_routing());
    const auto copper = drc::build_copper(board);
    CHECK(engine.disallowed(copper.items.front(), 0) == "named prohibition");
    route::Obstacles obstacles(board, rules);
    CHECK(obstacles.segment_state({4'000'000, 6'000'000}, {16'000'000, 6'000'000}, 0, 250'000, 1, false) == 2);
  }
}

TEST_CASE("unreadable conditions match conservatively and unknown exceptions cannot relax rules", "[rules][disallow]") {
  const auto board = capability_board();
  auto rules = capability_rules();
  rules.custom.push_back(prohibition("A.Type == 'Track'"));
  rules.custom.push_back(prohibition("false && A.Unknown == 0", "ignore"));
  drc::RuleEngine unknown_exception(board, rules);
  const auto copper = drc::build_copper(board);
  CHECK(warns(unknown_exception, "A.Unknown"));
  CHECK(unknown_exception.disallowed(copper.items.front(), 0).has_value());
  rules.custom.back().condition = "A.Type == 'Track'";
  drc::RuleEngine known_exception(board, rules);
  CHECK_FALSE(known_exception.disallowed(copper.items.front(), 0).has_value());
  rules.custom = {prohibition("A.Position_X <")};
  drc::RuleEngine malformed(board, rules);
  CHECK(warns(malformed, "cannot parse condition"));
  CHECK(malformed.disallowed(copper.items.front(), 0).has_value());
  rules.custom.back().origin = model::RuleOrigin::Synthetic;
  CHECK_THROWS_AS(drc::RuleEngine(board, rules), std::runtime_error);
}

TEST_CASE("uncertain numeric bounds never weaken known floors or caps regardless of file order", "[rules]") {
  const auto board = capability_board();
  const auto copper = drc::build_copper(board);
  for (bool uncertain_last : {false, true}) {
    auto rules = capability_rules();
    model::CustomRule known, uncertain;
    known.name = "known";
    known.constraints.push_back({"track_width", 200'000, {}, 800'000, {}});
    known.constraints.push_back({"length", 2'000'000, {}, 8'000'000, {}});
    uncertain.name = "uncertain";
    uncertain.condition = "false && A.Unknown == 1";
    uncertain.constraints.push_back({"track_width", 300'000, {}, 600'000, {}});
    uncertain.constraints.push_back({"length", 3'000'000, {}, 6'000'000, {}});
    rules.custom = uncertain_last ? std::vector{known, uncertain} : std::vector{uncertain, known};
    const drc::RuleEngine engine(board, rules);
    CHECK(engine.track_width(copper.items.front(), 0) == std::pair<Coord, Coord>{300'000, 600'000});
    const auto length = engine.length_constraint(1);
    CHECK(length.first == 3'000'000);
    CHECK(length.second == 6'000'000);
  }
}

TEST_CASE("supported but inapplicable item properties are undefined rather than unsupported", "[rules][disallow]") {
  const auto board = capability_board();
  for (const std::string expression : {"A.Pad_Type == 'SMD'", "A.Size_X > 0", "A.Reference != 'U1'", "B.Width > 0"}) {
    auto rules = capability_rules();
    rules.custom.push_back(prohibition(expression));
    const drc::RuleEngine engine(board, rules);
    CHECK(engine.warnings().empty());
    CHECK(engine.candidate_allowed(drc::build_copper(board).items.front(), 0));
  }
}

TEST_CASE("group and footprint ownership are exactly absent on new free copper", "[rules][disallow]") {
  const auto board = capability_board();
  drc::CopperItem candidate;
  candidate.kind = drc::ItemKind::Track;
  candidate.net = 1;
  candidate.layers = model::layer_bit(0);
  for (const std::string function : {"memberOfGroup", "memberOfFootprint"}) {
    auto rules = capability_rules();
    rules.custom.push_back(prohibition("A." + function + "('U1')"));
    const drc::RuleEngine engine(board, rules);
    CHECK(engine.candidate_allowed(candidate, 0));
    if (function == "memberOfGroup") CHECK(warns(engine, "A.memberOfGroup"));
    rules.custom.back().condition = "!A." + function + "('U1')";
    const drc::RuleEngine negated(board, rules);
    CHECK_FALSE(negated.candidate_allowed(candidate, 0));
  }
}

TEST_CASE("unsupported hole predicates cannot admit newly created vias", "[rules][disallow]") {
  const auto board = capability_board();
  auto rules = capability_rules();
  auto rule = prohibition("A.Type == 'Via'");
  rule.constraints.front().items = {"hole"};
  rules.custom.push_back(rule);
  const drc::RuleEngine engine(board, rules);
  REQUIRE(warns(engine, "unsupported disallow item hole"));
  drc::CopperItem via;
  via.kind = drc::ItemKind::Via;
  via.net = 1;
  via.layers = model::layer_bit(0) | model::layer_bit(1);
  CHECK_FALSE(engine.candidate_allowed(via, 0));
  via.kind = drc::ItemKind::Track;
  CHECK(engine.candidate_allowed(via, 0));
}

TEST_CASE("DRC disallow markers preserve rule identity and selected severity", "[rules][drc][disallow]") {
  const auto board = capability_board();
  auto rules = capability_rules();
  rules.custom.push_back(prohibition("A.Type == 'Track'", "warning"));
  const auto report = drc::run_drc(board, rules);
  const auto marker = std::find_if(report.violations.begin(), report.violations.end(), [](const auto& violation) {
    return violation.type == "items_not_allowed";
  });
  REQUIRE(marker != report.violations.end());
  CHECK(marker->rule == "named prohibition");
  CHECK(marker->description.find("named prohibition") != std::string::npos);
  CHECK(marker->severity == "warning");
}

TEST_CASE("bound forbidden-region leaves equal plain AST rules with holes, exemptions and precedence", "[rules][disallow]") {
  auto board = capability_board();
  board.nets.push_back({2, "GND"});
  model::Zone area;
  area.name = "box";
  area.rule_area = true;
  area.copper = model::layer_bit(0) | model::layer_bit(1);
  area.outline = {{{4'000'000, 2'000'000}, {16'000'000, 2'000'000}, {16'000'000, 8'000'000}, {4'000'000, 8'000'000}},
                  {{8'000'000, 4'000'000}, {12'000'000, 4'000'000}, {12'000'000, 6'000'000}, {8'000'000, 6'000'000}}};
  board.zones.push_back(area);
  for (const std::string predicate : {"A.intersectsArea('b*')", "A.insideArea('box')", "A.enclosedByArea('box')",
                                      "!A.intersectsArea('box')", "A.Position_X > 10mm && A.intersectsArea('box')"}) {
    auto rules = capability_rules();
    rules.custom.push_back(prohibition("A.NetName != 'GND' && (" + predicate + ")"));
    auto exception = prohibition("A.Position_Y > 7mm && A.Width < 0.4mm", "ignore");
    rules.custom.push_back(exception);
    const drc::RuleEngine engine(board, rules);
    REQUIRE(engine.warnings().empty());
    for (Coord x = 2'000'000; x <= 18'000'000; x += 500'000)
      for (Coord y = 1'000'000; y <= 9'000'000; y += 500'000)
        for (model::NetId net : {1, 2})
          for (int layer : {0, 1}) {
            drc::CopperItem candidate;
            candidate.net = net;
            candidate.kind = drc::ItemKind::Track;
            candidate.pos = {x, y};
            candidate.width = 200'000;
            candidate.anchor_layer = layer;
            candidate.layers = model::layer_bit(layer);
            candidate.shapes = {geom::Shape::segment({x, y}, {x + 2'000'000, y + 500'000}, 100'000)};
            candidate.box = candidate.shapes.front().box;
            REQUIRE(engine.candidate_allowed(candidate, layer) == engine.candidate_allowed_reference(candidate, layer));
          }
  }
}

TEST_CASE("class-uniform static disallow preserves caches, while differing same-class nets do not", "[rules][disallow]") {
  auto board = capability_board();
  board.nets.push_back({2, "GND"});
  auto rules = capability_rules();
  rules.custom.push_back(prohibition("A.NetClass == 'Default'"));
  CHECK_FALSE(drc::RuleEngine(board, rules).needs_exact_routing());
  rules.custom.back().condition = "A.NetName != 'GND'";
  CHECK(drc::RuleEngine(board, rules).needs_exact_routing());
  model::NetClass ground = rules.classes.front();
  ground.name = "Ground";
  rules.classes.push_back(ground);
  rules.assignments["GND"] = {"Ground"};
  CHECK_FALSE(drc::RuleEngine(board, rules).needs_exact_routing());
}

TEST_CASE("candidate legality enforces custom track bounds and via manufacturing floors", "[rules][route]") {
  auto board = capability_board();
  auto rules = capability_rules();
  model::CustomRule width;
  width.constraints.push_back({"track_width", 200'000, {}, 400'000, {}});
  rules.custom.push_back(width);
  rules.minimums.via_diameter = 600'000;
  rules.minimums.through_hole_diameter = 300'000;
  rules.minimums.via_annular_width = 100'000;
  route::Obstacles obstacles(board, rules);
  const geom::Point a{4'000'000, 6'000'000}, b{16'000'000, 6'000'000}, p{10'000'000, 6'000'000};
  CHECK(obstacles.segment_state(a, b, 0, 100'000, 1, false) == 2);
  CHECK(obstacles.segment_state(a, b, 0, 300'000, 1, false) == 0);
  CHECK(obstacles.segment_state(a, b, 0, 500'000, 1, false) == 2);
  for (const auto dimensions : {std::pair<Coord, Coord>{500'000, 300'000}, {600'000, 200'000},
                                {600'000, 500'000}, {600'000, 300'000}}) {
    const auto [diameter, drill] = dimensions;
    const bool legal = diameter == 600'000 && drill == 300'000;
    CHECK(obstacles.via_state(p, diameter, drill, 1, 0, false) == (legal ? 0 : 2));
    CHECK(obstacles.fixed_via_code(p, diameter, drill, 0, 1) == (legal ? route::Obstacles::kFree : route::Obstacles::kBlocked));
    CHECK(obstacles.fixed_via_code_reference(p, diameter, drill, 0, 1) == obstacles.fixed_via_code(p, diameter, drill, 0, 1));
  }
  auto odd_rules = capability_rules();
  odd_rules.minimums.track_width = 200'001;
  route::Obstacles odd_obstacles(board, odd_rules);
  CHECK(odd_obstacles.segment_state(a, b, 0, 200'001, 1, false) == 0);
  CHECK(odd_obstacles.disk_state(p, 0, 100'000, 1, 0, false) == 0);
  odd_rules.custom.push_back(prohibition("A.Width < 200001"));
  route::Obstacles conditional_width(board, odd_rules);
  CHECK(conditional_width.segment_state(a, b, 0, 200'001, 1, false) == 0);
  CHECK(conditional_width.segment_state(a, b, 0, 200'000, 1, false) == 2);
  CHECK(conditional_width.fixed_code(p, 0, 100'000, 0, 1) == route::Obstacles::kFree);
}
