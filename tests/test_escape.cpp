// SPDX-License-Identifier: GPL-3.0-or-later
// Escape planning (M9): corridor geometry on a synthetic BGA and the feasibility analysis on a fixture board.
#include <catch2/catch_test_macros.hpp>

#include <cmath>
#include <filesystem>
#include <algorithm>
#include <set>

#include "io/kicad/board_reader.hpp"
#include "io/kicad/project_reader.hpp"
#include "route/escape.hpp"
#include "route/escape_flow.hpp"
#include "route/obstacles.hpp"
#include "drc/copper.hpp"
#include "drc/connectivity.hpp"

using namespace tmk;
using geom::Point;

namespace {

// n x n ball grid at `pitch`, every ball on its own net, on F.Cu of a 2-layer board.
model::Board bga(int n, Coord pitch, Coord ball) {
  model::Board b;
  b.nets.push_back({});
  b.footprints.push_back({});
  b.footprints[0].reference = "U1";
  for (int y = 0; y < n; ++y)
    for (int x = 0; x < n; ++x) {
      model::Pad p;
      p.footprint = 0;
      p.number = std::to_string(y * n + x);
      p.type = model::PadType::Smd;
      p.shape = model::PadShape::Circle;
      p.pos = {x * pitch, y * pitch};
      p.size_x = p.size_y = ball;
      p.copper = model::layer_bit(0);
      model::Net net;
      net.name = "N" + p.number;
      p.net = static_cast<model::NetId>(b.nets.size());
      b.nets.push_back(net);
      b.footprints[0].pads.push_back(static_cast<int>(b.pads.size()));
      b.pads.push_back(p);
    }
  return b;
}

double dist_point_segment(Point p, Point a, Point b) {
  const double ux = static_cast<double>(b.x - a.x), uy = static_cast<double>(b.y - a.y);
  const double len2 = ux * ux + uy * uy;
  double t = len2 > 0 ? ((static_cast<double>(p.x - a.x)) * ux + (static_cast<double>(p.y - a.y)) * uy) / len2 : 0;
  t = std::clamp(t, 0.0, 1.0);
  return std::hypot(static_cast<double>(p.x - a.x) - t * ux, static_cast<double>(p.y - a.y) - t * uy);
}

model::DesignRules access_rules(Coord width = 150'000, Coord clearance = 10'000) {
  model::DesignRules rules;
  rules.classes.emplace_back();
  rules.classes[0].track_width = width;
  rules.classes[0].clearance = clearance;
  rules.classes[0].via_diameter = 450'000;
  rules.classes[0].via_drill = 300'000;
  rules.minimums.via_diameter = 450'000;
  rules.minimums.through_hole_diameter = 300'000;
  rules.minimums.via_annular_width = 75'000;
  return rules;
}

// One outstanding dense-package pad; the other balls are geometric neighbours, not routing obligations.
model::Board access_board() {
  auto b = bga(3, 600'000, 200'000);
  for (std::size_t i = 0; i < b.nets.size(); ++i) b.nets[i].id = static_cast<model::NetId>(i);
  b.layers = {{0, "F.Cu", "F.Cu", "signal", "", 0}, {31, "B.Cu", "B.Cu", "signal", "", 1}};
  b.copper = {0, 1};
  b.vias_tented = true;
  for (std::size_t i = 1; i < b.pads.size(); ++i) b.pads[i].net = 0;
  b.footprints.push_back({});
  b.footprints[1].reference = "J1";
  auto terminal = b.pads[0];
  terminal.footprint = 1;
  terminal.pos = {10'000'000, 0};
  b.footprints[1].pads.push_back(static_cast<int>(b.pads.size()));
  b.pads.push_back(terminal);
  return b;
}

void fill_rectangle(model::Board& b, int layer, Coord x0, Coord y0, Coord x1, Coord y1) {
  model::Zone zone;
  zone.net = 2;
  zone.copper = model::layer_bit(layer);
  zone.outline.push_back({{x0, y0}, {x1, y0}, {x1, y1}, {x0, y1}});
  zone.fills.emplace_back(layer, zone.outline.front());
  b.zones.push_back(std::move(zone));
}

void track_keepout(model::Board& b, model::LayerMask layers, Coord x0, Coord y0, Coord x1, Coord y1) {
  model::Zone zone;
  zone.copper = layers;
  zone.rule_area = true;
  zone.keepout_tracks = true;
  zone.keepout_vias = true;
  zone.outline.push_back({{x0, y0}, {x1, y0}, {x1, y1}, {x0, y1}});
  b.zones.push_back(std::move(zone));
}

// Equivalence compares a completed search domain, not two different enumeration costs.
constexpr long equivalence_work = 50'000'000;

void check_access_work(const route::AccessSearchResult& result, long budget = 0) {
  CHECK(result.work >= 0);
  CHECK(result.generation_work >= 0);
  CHECK(result.neighbor_work >= 0);
  CHECK(result.check_work >= 0);
  CHECK(result.expansion_work >= 0);
  CHECK(result.work == result.generation_work + result.neighbor_work + result.check_work + result.expansion_work);
  if (budget > 0) CHECK(result.work <= budget);
}

void check_router_access_work(const route::RouteResult& result, long budget) {
  CHECK(result.access_work >= 0);
  CHECK(result.access_generation_work >= 0);
  CHECK(result.access_neighbor_work >= 0);
  CHECK(result.access_check_work >= 0);
  CHECK(result.access_expansion_work >= 0);
  CHECK(result.access_lattice_work >= 0);
  CHECK(result.access_work == result.access_generation_work + result.access_neighbor_work +
        result.access_check_work + result.access_expansion_work + result.access_lattice_work);
  CHECK(result.access_work <= result.expansions);
  CHECK(result.access_work <= budget / 10);
  CHECK(result.expansions <= budget);
}

const route::PartEscape& dense_part(const std::vector<route::PartEscape>& parts) {
  const auto found = std::find_if(parts.begin(), parts.end(), [](const auto& part) { return part.footprint == 0; });
  REQUIRE(found != parts.end());
  return *found;
}

bool pad_contains(const model::Pad& pad, Point point) {
  const auto shapes = drc::pad_shapes(pad);
  return std::any_of(shapes.begin(), shapes.end(), [&](const auto& shape) {
    return geom::closer_than_disk(shape, point, 0, 1);
  });
}

void check_access_witness(const model::Board& b, route::Obstacles& obs, int pad, const route::AccessPath& path) {
  const auto& source = b.pads[static_cast<std::size_t>(pad)];
  REQUIRE((!path.steps.empty() || !path.vias.empty()));
  Point cursor = path.steps.empty() ? path.end : path.steps.front().a;
  int layer = path.steps.empty() ? path.layer : path.steps.front().layer;
  CHECK(pad_contains(source, cursor));
  CHECK(((source.copper & model::layer_bit(layer)) != 0 ||
        std::any_of(path.vias.begin(), path.vias.end(), [&](const auto& via) {
          if (via.pos != cursor || layer < via.layer_top || layer > via.layer_bottom) return false;
          for (int l = via.layer_top; l <= via.layer_bottom; ++l)
            if (source.copper & model::layer_bit(l)) return true;
          return false;
        })));
  for (const auto& step : path.steps) {
    CHECK(step.a == cursor);
    if (step.layer != layer) {
      const auto via = std::find_if(path.vias.begin(), path.vias.end(), [&](const auto& v) {
        return v.pos == cursor && v.layer_top <= std::min(layer, step.layer) &&
               v.layer_bottom >= std::max(layer, step.layer);
      });
      CHECK(via != path.vias.end());
    }
    CHECK(step.width > 0);
    CHECK(obs.segment_state(step.a, step.b, step.layer, step.width, source.net, false) == 0);
    cursor = step.b;
    layer = step.layer;
  }
  CHECK(cursor == path.end);
  if (layer != path.layer) {
    CHECK(std::any_of(path.vias.begin(), path.vias.end(), [&](const auto& v) {
      return v.pos == cursor && v.layer_top <= std::min(layer, path.layer) &&
             v.layer_bottom >= std::max(layer, path.layer);
    }));
  }
  for (const auto& via : path.vias) {
    CHECK(via.net == source.net);
    CHECK(obs.via_state_span(via.pos, via.size, via.drill, via.net, 0, false, nullptr,
                             via.layer_top, via.layer_bottom, via.type) == 0);
    CHECK(((path.steps.empty() && via.pos == path.end) || std::any_of(path.steps.begin(), path.steps.end(), [&](const auto& step) {
      return step.a == via.pos || step.b == via.pos;
    })));
  }
}

void check_same_access(const route::AccessSearchResult& indexed, const route::AccessSearchResult& reference) {
  check_access_work(indexed);
  check_access_work(reference);
  REQUIRE(indexed.candidates.size() == reference.candidates.size());
  for (std::size_t i = 0; i < indexed.candidates.size(); ++i) {
    CHECK(indexed.candidates[i].first == reference.candidates[i].first);
    CHECK(indexed.candidates[i].second == reference.candidates[i].second);
  }
  CHECK(indexed.exhausted == reference.exhausted);
  REQUIRE(indexed.paths.size() == reference.paths.size());
  for (std::size_t i = 0; i < indexed.paths.size(); ++i) {
    const auto& a = indexed.paths[i];
    const auto& b = reference.paths[i];
    CHECK(a.end == b.end);
    CHECK(a.layer == b.layer);
    CHECK(a.cost == b.cost);
    REQUIRE(a.steps.size() == b.steps.size());
    for (std::size_t k = 0; k < a.steps.size(); ++k) {
      CHECK(a.steps[k].a == b.steps[k].a);
      CHECK(a.steps[k].b == b.steps[k].b);
      CHECK(a.steps[k].layer == b.steps[k].layer);
      CHECK(a.steps[k].width == b.steps[k].width);
    }
    REQUIRE(a.vias.size() == b.vias.size());
    for (std::size_t k = 0; k < a.vias.size(); ++k) {
      CHECK(a.vias[k].pos == b.vias[k].pos);
      CHECK(a.vias[k].size == b.vias[k].size);
      CHECK(a.vias[k].drill == b.vias[k].drill);
      CHECK(a.vias[k].type == b.vias[k].type);
      CHECK(a.vias[k].layer_top == b.vias[k].layer_top);
      CHECK(a.vias[k].layer_bottom == b.vias[k].layer_bottom);
      CHECK(a.vias[k].net == b.vias[k].net);
    }
  }
}

}  // namespace

TEST_CASE("escape plan: perimeter pins fan out, inner balls get one dog-bone site each", "[escape]") {
  const Coord pitch = 800'000;
  const auto b = bga(6, pitch, 400'000);
  std::vector<char> needs(b.pads.size(), 1);
  route::EscapeStats st;
  const auto plan = route::plan_escapes(b, needs, [](model::NetId) { return Coord{300'000}; }, {}, &st);
  REQUIRE(plan.size() == b.pads.size());
  CHECK(st.parts == 1);
  CHECK(st.perimeter == 20);  // the outer ring of a 6 x 6 array
  CHECK(st.dogbones == 16);
  std::set<std::pair<Coord, Coord>> sites;
  const Point centre{5 * pitch / 2, 5 * pitch / 2};
  for (const auto& c : plan) {
    CHECK(c.band <= pitch / 2);
    if (c.via) {
      CHECK(sites.insert({c.b.x, c.b.y}).second);  // every interstitial site serves one ball
      // pointing away from the package centre
      CHECK(std::llabs(c.b.x - centre.x) >= std::llabs(c.a.x - centre.x));
      CHECK(std::llabs(c.b.y - centre.y) >= std::llabs(c.a.y - centre.y));
    } else {
      // leaves the package
      const bool out = c.b.x < 0 || c.b.y < 0 || c.b.x > 5 * pitch || c.b.y > 5 * pitch;
      CHECK(out);
    }
  }
  // No corridor runs over another ball's centre (corridors never reserve a neighbour's pad).
  for (const auto& c : plan)
    for (const auto& p : b.pads)
      if (b.pads[static_cast<std::size_t>(c.pad)].pos != p.pos) CHECK(dist_point_segment(p.pos, c.a, c.b) >= static_cast<double>(pitch) / 2 - 1);
  // With a channel callback the second ring escapes between two outer balls instead of taking a via.
  route::EscapeStats st2;
  const auto plan2 = route::plan_escapes(b, needs, [](model::NetId) { return Coord{300'000}; }, {}, &st2,
                                         [](model::NetId) { return Coord{350'000}; });  // 0.8 pitch - 0.4 ball = 0.4 >= 0.35
  CHECK(st2.perimeter == 20);
  CHECK(st2.second_ring == 12);
  CHECK(st2.dogbones == 4);
  std::set<std::pair<Coord, Coord>> gaps;
  for (const auto& c : plan2)
    if (c.has_mid) {
      CHECK(gaps.insert({c.mid.x, c.mid.y}).second);  // one ball per gap
      for (const auto& p : b.pads)
        if (b.pads[static_cast<std::size_t>(c.pad)].pos != p.pos) {
          CHECK(dist_point_segment(p.pos, c.a, c.mid) >= static_cast<double>(pitch) / 2 - 1);
          CHECK(dist_point_segment(p.pos, c.mid, c.b) >= static_cast<double>(pitch) / 2 - 1);
        }
    }
  // Too narrow a gap: dog-bones as before.
  route::EscapeStats st3;
  route::plan_escapes(b, needs, [](model::NetId) { return Coord{300'000}; }, {}, &st3, [](model::NetId) { return Coord{450'000}; });
  CHECK(st3.second_ring == 0);
  // Deterministic.
  const auto again = route::plan_escapes(b, needs, [](model::NetId) { return Coord{300'000}; });
  REQUIRE(again.size() == plan.size());
  for (std::size_t i = 0; i < plan.size(); ++i) CHECK((again[i].b == plan[i].b && again[i].pad == plan[i].pad));
  // Pads that need no routing get no corridor; coarse parts are left alone.
  std::vector<char> none(b.pads.size(), 0);
  CHECK(route::plan_escapes(b, none, [](model::NetId) { return Coord{300'000}; }).empty());
  route::EscapeOptions coarse;
  coarse.max_pitch = 500'000;
  CHECK(route::plan_escapes(b, needs, [](model::NetId) { return Coord{300'000}; }, coarse).empty());
}

TEST_CASE("escape analysis: fixture verdicts stay domain-qualified and witnesses exact", "[escape][fixture]") {
  const std::string path = std::string(TM_SOURCE_DIR) + "/bench/data/freerouting/scripts/benchmark/fixtures/PCBench/sbc_sbc/unrouted.kicad_pcb";
  if (!std::filesystem::exists(path)) SKIP("fixture missing: " + path);
  const auto lb = io::read_board_file(path);
  const auto rules = io::read_design_rules(path);
  model::Board b = lb.board;
  route::Obstacles obs(b, rules);
  const Coord mask = obs.via_mask();
  const auto parts = route::analyse_escapes(b, rules, obs);
  const route::PartEscape* dram = nullptr;
  for (const auto& pe : parts)
    if (pe.ref == "DRAM1") dram = &pe;
  REQUIRE(dram != nullptr);
  CHECK(dram->results.size() > 60);
  const auto check_results = [&](const route::PartEscape& part) {
    int witnessed = 0;
    for (const auto& pin : part.results) {
      CHECK_FALSE(pin.domain.empty());
      CHECK((pin.status == "satisfied" || pin.status == "witness" || pin.status == "exhausted" || pin.status == "unknown"));
      if (pin.status == "witness") {
        ++witnessed;
        check_access_witness(b, obs, pin.pad, pin.witness);
      }
    }
    CHECK(witnessed == part.escapable);
    for (const auto& dead : part.dead) {
      CHECK(std::any_of(part.results.begin(), part.results.end(), [&](const auto& pin) {
        return pin.pad == dead.pad && pin.status == "exhausted";
      }));
    }
  };
  check_results(*dram);
  // The configured mask is not changed by the analysis.
  CHECK(mask > 0);
  CHECK(obs.via_mask() == mask);  // analysing did not modify the selected rule domain
  obs.set_via_mask(0);
  const auto tented = route::analyse_escapes(b, rules, obs);
  for (const auto& part : tented)
    if (part.ref == "DRAM1") check_results(part);
}

TEST_CASE("escape analysis skips already-connected pads, including an internally satisfied net", "[escape]") {
  auto b = access_board();
  const auto rules = access_rules();
  SECTION("existing locked route to a remote terminal") {
    b.tracks.push_back({b.pads[0].pos, b.pads.back().pos, 150'000, 0, 1, true});
  }
  SECTION("both required terminals are inside the package") {
    b.pads.back().net = 0;
    b.pads[1].net = 1;
    b.tracks.push_back({b.pads[0].pos, b.pads[1].pos, 150'000, 0, 1, true});
  }
  route::Obstacles obs(b, rules);
  const auto parts = route::analyse_escapes(b, rules, obs);
  const auto& part = dense_part(parts);
  CHECK(part.pins == 0);
  CHECK(part.escapable == 0);
  CHECK(part.dead.empty());
  REQUIRE_FALSE(part.results.empty());
  for (const auto& pin : part.results) {
    CHECK(pin.status == "satisfied");
    CHECK(pin.witness.steps.empty());
    CHECK(pin.witness.vias.empty());
  }
  REQUIRE(b.tracks.size() == 1);
  CHECK(b.tracks[0].locked);
}

TEST_CASE("escape obligations include reachable unused planes only in refillable mode", "[escape][soft-zones]") {
  auto b = access_board();
  const auto rules = access_rules();
  b.pads.back().net = 0;
  fill_rectangle(b, 1, -2'000'000, -2'000'000, 2'000'000, 2'000'000);
  b.zones.back().net = 1;
  route::Obstacles hard(b, rules);
  const auto fixed = route::analyse_escapes(b, rules, hard);
  CHECK(dense_part(fixed).pins == 0);
  CHECK(dense_part(fixed).results.front().status == "satisfied");
  route::Obstacles soft(b, rules, true);
  route::EscapeAnalysisOptions options;
  options.routing.soft_zones = true;
  const auto refillable = route::analyse_escapes(b, rules, soft, options);
  CHECK(dense_part(refillable).pins == 1);
  CHECK(dense_part(refillable).results.front().status == "witness");
}

TEST_CASE("escape analysis distinguishes immutable fills from refillable zones", "[escape][soft-zones]") {
  auto b = access_board();
  const auto rules = access_rules();
  for (int layer = 0; layer < b.copper_count(); ++layer) {
    fill_rectangle(b, layer, -3'000'000, -3'000'000, -400'000, 3'000'000);
    fill_rectangle(b, layer, 1'600'000, -3'000'000, 3'000'000, 3'000'000);
    fill_rectangle(b, layer, -400'000, -3'000'000, 1'600'000, -400'000);
    fill_rectangle(b, layer, -400'000, 1'600'000, 1'600'000, 3'000'000);
  }
  route::EscapeAnalysisOptions options;
  options.work_budget = equivalence_work;
  options.routing.allow_vias = false;
  route::Obstacles hard(b, rules);
  const auto hard_parts = route::analyse_escapes(b, rules, hard, options);
  const auto& hard_part = dense_part(hard_parts);
  REQUIRE(hard_part.results.size() == 1);
  CHECK(hard_part.results[0].status == "exhausted");
  CHECK(hard_part.escapable == 0);
  REQUIRE(hard_part.dead.size() == 1);
  CHECK_FALSE(hard_part.results[0].domain.empty());
  route::Obstacles soft(b, rules, true);
  options.routing.soft_zones = true;
  const auto soft_parts = route::analyse_escapes(b, rules, soft, options);
  const auto& soft_part = dense_part(soft_parts);
  REQUIRE(soft_part.results.size() == 1);
  CHECK(soft_part.results[0].status == "witness");
  CHECK(soft_part.escapable == 1);
  CHECK(soft_part.dead.empty());
  CHECK(soft_part.results[0].domain != hard_part.results[0].domain);
  check_access_witness(b, soft, 0, soft_part.results[0].witness);
  options.reference = true;
  const auto reference = route::analyse_escapes(b, rules, soft, options);
  const auto& ref_part = dense_part(reference);
  REQUIRE(ref_part.results.size() == soft_part.results.size());
  CHECK(ref_part.results[0].status == soft_part.results[0].status);
  CHECK(ref_part.results[0].domain == soft_part.results[0].domain);
}

TEST_CASE("escape analysis work exhaustion is unknown, never a dead-pin proof", "[escape]") {
  auto b = access_board();
  const auto rules = access_rules();
  route::Obstacles obs(b, rules);
  route::EscapeAnalysisOptions options;
  for (bool reference : {false, true}) {
    options.reference = reference;
    for (long cap : {1L, 16L, 64L}) {
      CAPTURE(reference, cap);
      options.work_budget = cap;
      const auto parts = route::analyse_escapes(b, rules, obs, options);
      const auto& part = dense_part(parts);
      REQUIRE(part.results.size() == 1);
      CHECK(part.results[0].status == "unknown");
      CHECK_FALSE(part.results[0].reason.empty());
      CHECK_FALSE(part.results[0].domain.empty());
      CHECK(part.dead.empty());
    }
  }
}

TEST_CASE("escape analysis seeds only real copper after package rotation and side changes", "[escape]") {
  auto b = access_board();
  const auto rules = access_rules();
  double angle = 37;
  int layer = 0;
  SECTION("front") {}
  SECTION("flipped") {
    angle = 127;
    layer = 1;
    b.footprints[0].back = true;
  }
  const Point translation{1'010'010, 1'010'010};
  const double radians = angle * std::acos(-1.0) / 180.0;
  for (auto& pad : b.pads) {
    const Point original = pad.pos;
    pad.pos = {translation.x + static_cast<Coord>(std::llround(static_cast<double>(original.x) * std::cos(radians) - static_cast<double>(original.y) * std::sin(radians))),
               translation.y + static_cast<Coord>(std::llround(static_cast<double>(original.x) * std::sin(radians) + static_cast<double>(original.y) * std::cos(radians)))};
    pad.angle = angle;
    pad.copper = model::layer_bit(layer);
  }
  b.pads[0].shape = model::PadShape::Rect;
  b.pads[0].size_x = 700'000;
  b.pads[0].size_y = 200'000;
  route::Obstacles obs(b, rules);
  route::EscapeAnalysisOptions options;
  options.work_budget = equivalence_work;
  options.routing.allow_vias = false;
  const auto parts = route::analyse_escapes(b, rules, obs, options);
  const auto& part = dense_part(parts);
  REQUIRE(part.results.size() == 1);
  REQUIRE(part.results[0].status == "witness");
  check_access_witness(b, obs, 0, part.results[0].witness);
  options.reference = true;
  const auto reference = route::analyse_escapes(b, rules, obs, options);
  const auto& ref_part = dense_part(reference);
  REQUIRE(ref_part.results.size() == 1);
  CHECK(ref_part.results[0].status == part.results[0].status);
  CHECK(ref_part.results[0].domain == part.results[0].domain);
  check_same_access({{part.results[0].witness}, false, 0, {}}, {{ref_part.results[0].witness}, false, 0, {}});
}

TEST_CASE("access graph seeds rotated and flipped pads on their real copper", "[escape][access]") {
  auto b = access_board();
  const auto rules = access_rules();
  auto& pad = b.pads[0];
  pad.pos = {1'010'010, 1'010'010};
  pad.shape = model::PadShape::Rect;
  pad.size_x = 700'000;
  pad.size_y = 200'000;
  SECTION("rotated front pad") { pad.angle = 37; }
  SECTION("rotated flipped pad") {
    b.footprints[0].back = true;
    b.footprints[0].angle = 90;
    pad.angle = 127;
    pad.copper = model::layer_bit(1);
  }
  for (std::size_t i = 1; i < b.pads.size(); ++i) b.pads[i].pos.x += 20'000'000;
  route::Obstacles obs(b, rules);
  route::AccessSearchOptions options;
  options.work_budget = equivalence_work;
  options.record_candidates = true;
  options.routing.allow_vias = false;
  options.target = [centre = pad.pos](Point p, int, const std::function<bool()>&) {
    return std::llabs(p.x - centre.x) >= 600'000 || std::llabs(p.y - centre.y) >= 600'000 ? route::AccessGoal::Endpoint : route::AccessGoal::None;
  };
  const auto indexed = route::generate_access_paths(b, rules, obs, 0, options);
  REQUIRE_FALSE(indexed.paths.empty());
  for (const auto& path : indexed.paths) {
    check_access_witness(b, obs, 0, path);
    CHECK(options.target(path.end, path.layer, [] { return true; }) != route::AccessGoal::None);
  }
  options.reference = true;
  const auto reference = route::generate_access_paths(b, rules, obs, 0, options);
  check_same_access(indexed, reference);
}

TEST_CASE("access graph finds a via pocket between global lattice sites", "[escape][access]") {
  auto b = access_board();
  const auto rules = access_rules();
  const Point pocket{1'010'010, 1'010'010};
  b.pads[0].pos = {pocket.x - 500'000, pocket.y};
  for (std::size_t i = 1; i < b.pads.size(); ++i) b.pads[i].pos.x += 20'000'000;
  const Coord left = pocket.x - 235'000, right = pocket.x + 235'000;
  const Coord bottom = pocket.y - 235'000, top = pocket.y + 235'000;
  // F.Cu has a 0.2 mm access tunnel feeding the square cavity. B.Cu has only the cavity.
  fill_rectangle(b, 0, -4'000'000, pocket.y + 100'000, left, 6'000'000);
  fill_rectangle(b, 0, -4'000'000, -4'000'000, left, pocket.y - 100'000);
  fill_rectangle(b, 0, left, top, 6'000'000, 6'000'000);
  fill_rectangle(b, 0, left, -4'000'000, 6'000'000, bottom);
  fill_rectangle(b, 0, right, bottom, 6'000'000, top);
  fill_rectangle(b, 1, -4'000'000, -4'000'000, left, 6'000'000);
  fill_rectangle(b, 1, right, -4'000'000, 6'000'000, 6'000'000);
  fill_rectangle(b, 1, left, -4'000'000, right, bottom);
  fill_rectangle(b, 1, left, top, right, 6'000'000);
  route::Obstacles obs(b, rules);
  REQUIRE(obs.via_state(pocket, 450'000, 300'000, 1, 0, false) == 0);
  CHECK(obs.via_state({1'000'000, 1'000'000}, 450'000, 300'000, 1, 0, false) == 2);
  route::AccessSearchOptions options;
  options.work_budget = equivalence_work;
  options.record_candidates = true;
  options.origin = {0, 0};
  options.target = [](Point, int layer, const std::function<bool()>&) { return layer == 1 ? route::AccessGoal::Endpoint : route::AccessGoal::None; };
  const auto indexed = route::generate_access_paths(b, rules, obs, 0, options);
  REQUIRE_FALSE(indexed.paths.empty());
  for (const auto& path : indexed.paths) {
    check_access_witness(b, obs, 0, path);
    CHECK(path.layer == 1);
    REQUIRE_FALSE(path.vias.empty());
    CHECK(std::any_of(path.vias.begin(), path.vias.end(), [&](const auto& via) {
      return (via.pos.x - options.origin.x) % options.pitch != 0 ||
             (via.pos.y - options.origin.y) % options.pitch != 0;
    }));
  }
  options.reference = true;
  check_same_access(indexed, route::generate_access_paths(b, rules, obs, 0, options));
  SECTION("the router commits the same off-grid access transactionally") {
    b.pads.back().pos = pocket;
    b.pads.back().copper = model::layer_bit(1);
    route::RouterOptions routing;
    routing.pitch = options.pitch;
    routing.work_budget = 2'000'000;
    routing.time_limit_s = 30;
    routing.gpu_device = -1;
    routing.optimize = false;
    const auto routed = route::Router(b, rules, routing).run();
    REQUIRE(routed.connections == 1);
    REQUIRE(routed.routed == 1);
    REQUIRE(routed.access_connections == 1);
    REQUIRE_FALSE(routed.vias.empty());
    check_router_access_work(routed, routing.work_budget);
    CHECK(std::any_of(routed.vias.begin(), routed.vias.end(), [&](const auto& v) {
      return v.pos.x % options.pitch != 0 || v.pos.y % options.pitch != 0;
    }));
    for (const auto& track : routed.tracks)
      CHECK(obs.segment_state(track.a, track.b, track.layer, track.width, track.net, false) == 0);
    for (const auto& v : routed.vias)
      CHECK(obs.via_state(v.pos, v.size, v.drill, v.net, 0, false) == 0);
  }
}

TEST_CASE("router carries off-grid via access through a narrow corridor to a real lattice exit",
          "[escape][access][route][access-consumer]") {
  auto b = access_board();
  auto rules = access_rules(147'000, 10'000);
  auto fine = rules.classes[0];
  fine.name = "declared fine width";
  fine.track_width = 127'000;
  rules.classes.push_back(fine);
  model::CustomRule through_only;
  through_only.name = "access vias must retain their actual subtype";
  through_only.condition = "A.Type == 'Via'";
  model::Constraint via_constraint;
  via_constraint.type = "disallow";
  via_constraint.items = {"blind_via", "buried_via"};
  through_only.constraints.push_back(via_constraint);
  rules.custom.push_back(through_only);
  const Point pocket{1'010'010, 1'005'010};
  const Coord corridor_end = pocket.x + 2'400'000;
  const Coord footprint_exit = pocket.x + 2'000'000;
  auto source = b.pads[0];
  source.pos = {pocket.x - 500'000, pocket.y};
  source.size_x = source.size_y = 100'000;
  auto terminal = b.pads.back();
  terminal.pos = {pocket.x + 2'800'000, pocket.y};
  terminal.size_x = terminal.size_y = 100'000;
  terminal.copper = model::layer_bit(1);
  // A same-plane package pad makes the required exit extend beyond the via pocket and narrow corridor.
  // Unlike a disconnected dummy signal pin, it creates no additional routing obligation.
  auto plane_pad = source;
  plane_pad.number = "GND";
  plane_pad.net = 2;
  plane_pad.pos = {pocket.x + 1'500'000, pocket.y + 2'000'000};
  b.pads = {source, terminal, plane_pad};
  b.footprints[0].pads = {0, 2};
  b.footprints[1].pads = {1};
  bool requires_neck_endpoint = false;
  SECTION("variable-width access exits into preferred-width free space") {}
  SECTION("the only lattice endpoint needs a freshly generated neck-width access") {
    requires_neck_endpoint = true;
    b.pads[1].pos.x = pocket.x + 4'500'000;  // Beyond the four-millimetre graph, so a lattice hand-off is required.
  }
  const Coord left = pocket.x - 235'000, right = pocket.x + 235'000;
  const Coord bottom = pocket.y - 235'000, top = pocket.y + 235'000;
  // F.Cu is a closed 0.2 mm tunnel ending in a cavity that fits the legal via only off-grid.
  fill_rectangle(b, 0, -4'000'000, -4'000'000, source.pos.x - 235'000, 6'000'000);
  fill_rectangle(b, 0, source.pos.x - 235'000, pocket.y + 100'000, left, 6'000'000);
  fill_rectangle(b, 0, source.pos.x - 235'000, -4'000'000, left, pocket.y - 100'000);
  fill_rectangle(b, 0, left, top, 6'000'000, 6'000'000);
  fill_rectangle(b, 0, left, -4'000'000, 6'000'000, bottom);
  fill_rectangle(b, 0, right, bottom, 6'000'000, top);
  // B.Cu has the same cavity, followed by a 0.16 mm corridor: 0.147 mm + 2*0.01 mm cannot fit,
  // but the explicitly declared 0.127 mm width can. Keep every saved fill immutable.
  fill_rectangle(b, 1, -4'000'000, -4'000'000, left, 6'000'000);
  fill_rectangle(b, 1, left, -4'000'000, right, bottom);
  fill_rectangle(b, 1, left, top, right, 6'000'000);
  const Coord wall_end = requires_neck_endpoint ? 6'000'000 : corridor_end;
  fill_rectangle(b, 1, right, -4'000'000, wall_end, pocket.y - 80'000);
  fill_rectangle(b, 1, right, pocket.y + 80'000, wall_end, 6'000'000);
  const auto original_zones = b.zones;
  route::Obstacles obs(b, rules);
  REQUIRE(obs.via_state(pocket, 450'000, 300'000, 1, 0, false) == 0);
  REQUIRE(obs.via_state({1'000'000, 1'000'000}, 450'000, 300'000, 1, 0, false) == 2);
  REQUIRE(obs.segment_state({right + 100'000, pocket.y}, {footprint_exit + 100'000, pocket.y},
                            1, 147'000, 1, false) == 2);
  REQUIRE(obs.segment_state({right + 100'000, pocket.y}, {footprint_exit + 100'000, pocket.y},
                            1, 127'000, 1, false) == 0);
  if (requires_neck_endpoint) {
    REQUIRE(obs.disk_state({3'200'000, 1'000'000}, 1, 147'000 / 2, 1, 0, false) == 2);
    REQUIRE(obs.disk_state({3'200'000, 1'000'000}, 1, 127'000 / 2, 1, 0, false) == 0);
  }
  route::RouterOptions options;
  options.pitch = 40'000;
  options.work_budget = 20'000'000;
  options.time_limit_s = 600;
  options.gpu_device = -1;
  options.optimize = false;
  options.soft_zones = false;
  const auto result = route::Router(b, rules, options).run();
  REQUIRE(result.connections == 1);
  REQUIRE(result.routed == 1);
  REQUIRE(result.access_connections == 1);
  CHECK(result.narrowed_access == 1);
  CHECK(result.failures.empty());
  CHECK(result.unrouted.empty());
  CHECK(result.rips == 0);
  REQUIRE_FALSE(result.vias.empty());
  CHECK(std::any_of(result.vias.begin(), result.vias.end(), [](const auto& via) {
    return via.pos.x % 40'000 != 0 || via.pos.y % 40'000 != 0;
  }));
  CHECK(std::any_of(result.tracks.begin(), result.tracks.end(), [&](const auto& track) {
    return track.layer == 1 && track.width < 147'000 && std::max(track.a.x, track.b.x) > footprint_exit;
  }));
  if (requires_neck_endpoint) CHECK(result.necked > 0);
  check_router_access_work(result, options.work_budget);
  for (const auto& track : result.tracks) {
    CHECK(track.net == 1);
    CHECK(track.width >= 127'000);
    CHECK(obs.segment_state(track.a, track.b, track.layer, track.width, track.net, false) == 0);
  }
  for (const auto& via : result.vias) {
    CHECK(via.net == 1);
    CHECK(obs.via_state_span(via.pos, via.size, via.drill, via.net, 0, false, nullptr,
                             via.layer_top, via.layer_bottom, via.type) == 0);
  }
  auto routed = b;
  routed.tracks.insert(routed.tracks.end(), result.tracks.begin(), result.tracks.end());
  routed.vias.insert(routed.vias.end(), result.vias.begin(), result.vias.end());
  REQUIRE(routed.zones.size() == original_zones.size());
  for (std::size_t i = 0; i < original_zones.size(); ++i)
    CHECK(routed.zones[i].fills == original_zones[i].fills);
  route::Obstacles committed(routed, rules);
  const auto connected = drc::compute_connectivity(routed, committed.copper(), committed.grid());
  int source_root = -1, terminal_root = -1;
  for (std::size_t i = 0; i < committed.copper().items.size(); ++i) {
    const auto& item = committed.copper().items[i];
    if (item.kind != drc::ItemKind::Pad) continue;
    if (item.index == 0) source_root = connected.root[i];
    if (item.index == 1) terminal_root = connected.root[i];
  }
  REQUIRE(source_root >= 0);
  REQUIRE(terminal_root >= 0);
  CHECK(source_root == terminal_root);
  // A reported success consisting only of the access prefix cannot satisfy the actual copper topology.
  for (std::size_t i = 0; i < committed.copper().items.size(); ++i)
    if (committed.copper().items[i].net == 1) CHECK(connected.root[i] == source_root);
  CHECK(b.tracks.empty());
  CHECK(b.vias.empty());
}

TEST_CASE("access widths use hard constraints, not the 0.147 mm class target as a floor", "[escape][access]") {
  auto b = access_board();
  auto rules = access_rules(147'000, 200'000);
  // As on CM5, the designer has also declared a 0.127 mm class; zero board minimum is not
  // permission to invent an infinitesimal manufacturing floor.
  auto fine = rules.classes[0];
  fine.name = "100ohm";
  fine.track_width = 127'000;
  rules.classes.push_back(fine);
  b.pads[0].size_x = b.pads[0].size_y = 100'000;
  for (std::size_t i = 1; i < b.pads.size(); ++i) b.pads[i].pos.x += 20'000'000;
  fill_rectangle(b, 0, -5'000'000, 270'000, 5'000'000, 5'000'000);
  fill_rectangle(b, 0, -5'000'000, -5'000'000, 5'000'000, -270'000);
  bool narrowing_allowed = true;
  SECTION("the board permits a narrower legal access") {}
  SECTION("a hard custom floor between declared floor and target still permits access") {
    model::CustomRule rule;
    rule.name = "N0 minimum access width";
    rule.condition = "A.NetName == 'N0'";
    model::Constraint constraint;
    constraint.type = "track_width";
    constraint.min = 130'000;
    rule.constraints.push_back(constraint);
    rules.custom.push_back(rule);
  }
  SECTION("the board forbids narrowing below 0.147 mm") {
    rules.minimums.track_width = 147'000;
    narrowing_allowed = false;
  }
  SECTION("a net-specific custom minimum forbids narrowing below 0.147 mm") {
    model::CustomRule rule;
    rule.name = "N0 minimum access width";
    rule.condition = "A.NetName == 'N0'";
    model::Constraint constraint;
    constraint.type = "track_width";
    constraint.min = 147'000;
    rule.constraints.push_back(constraint);
    rules.custom.push_back(rule);
    narrowing_allowed = false;
  }
  route::Obstacles obs(b, rules);
  CHECK(obs.segment_state({0, 0}, {1'000'000, 0}, 0, 147'000, 1, false) == 2);
  // Exact checks enforce hard width minima (D65), so 0.140 mm is legal only where narrowing is allowed.
  CHECK((obs.segment_state({0, 0}, {1'000'000, 0}, 0, 140'000, 1, false) == 0) == narrowing_allowed);
  CHECK(obs.segment_state({0, 0}, {1'000'000, 0}, 0, 140'002, 1, false) == 2);
  route::AccessSearchOptions options;
  options.work_budget = equivalence_work;
  options.record_candidates = true;
  options.routing.allow_vias = false;
  options.target = [](Point p, int layer, const std::function<bool()>&) {
    return layer == 0 && p.x >= 1'000'000 ? route::AccessGoal::Endpoint : route::AccessGoal::None;
  };
  const auto indexed = route::generate_access_paths(b, rules, obs, 0, options);
  if (narrowing_allowed) {
    REQUIRE_FALSE(indexed.paths.empty());
    for (const auto& path : indexed.paths) {
      check_access_witness(b, obs, 0, path);
      CHECK(options.target(path.end, path.layer, [] { return true; }) != route::AccessGoal::None);
      // An odd integer width shares the same width/2 exact radius; the next radius (140002) is blocked.
      CHECK(std::any_of(path.steps.begin(), path.steps.end(), [](const auto& step) { return step.width <= 140'001; }));
    }
  } else {
    CHECK(indexed.paths.empty());
    CHECK(indexed.exhausted);
  }
  options.reference = true;
  check_same_access(indexed, route::generate_access_paths(b, rules, obs, 0, options));
}

TEST_CASE("access graph retains multibend escapes beyond 0.8 mm and never rips locked copper", "[escape][access]") {
  auto b = access_board();
  const auto rules = access_rules();
  for (std::size_t i = 1; i < b.pads.size(); ++i) b.pads[i].pos.x += 20'000'000;
  fill_rectangle(b, 0, -10'000'000, 900'000, 10'000'000, 10'000'000);
  fill_rectangle(b, 0, -10'000'000, -10'000'000, 10'000'000, -900'000);
  b.tracks.push_back({{700'000, -200'000}, {700'000, 900'000}, 200'000, 0, 2, true});
  b.tracks.push_back({{1'300'000, -900'000}, {1'300'000, 200'000}, 200'000, 0, 2, true});
  const auto original = b.tracks;
  route::Obstacles obs(b, rules);
  CHECK(obs.segment_state({0, 0}, {2'000'000, 0}, 0, 150'000, 1, false) == 2);
  route::AccessSearchOptions options;
  options.work_budget = equivalence_work;
  options.record_candidates = true;
  options.radius = 4'000'000;
  options.routing.allow_vias = false;
  options.target = [](Point p, int layer, const std::function<bool()>&) {
    return layer == 0 && p.x >= 2'000'000 && std::llabs(p.y) < 200'000 ? route::AccessGoal::Endpoint : route::AccessGoal::None;
  };
  const auto indexed = route::generate_access_paths(b, rules, obs, 0, options);
  REQUIRE_FALSE(indexed.paths.empty());
  for (const auto& path : indexed.paths) {
    check_access_witness(b, obs, 0, path);
    CHECK(options.target(path.end, path.layer, [] { return true; }) != route::AccessGoal::None);
    REQUIRE(path.steps.size() >= 3);
    bool below = false, above = false;
    for (const auto& step : path.steps) {
      below |= step.a.y < -200'000 || step.b.y < -200'000;
      above |= step.a.y > 200'000 || step.b.y > 200'000;
    }
    CHECK(below);
    CHECK(above);
    CHECK(path.end.x - b.pads[0].pos.x > 800'000);
    CHECK(path.vias.empty());
  }
  options.reference = true;
  check_same_access(indexed, route::generate_access_paths(b, rules, obs, 0, options));
  REQUIRE(b.tracks.size() == original.size());
  for (std::size_t i = 0; i < original.size(); ++i) {
    CHECK(b.tracks[i].a == original[i].a);
    CHECK(b.tracks[i].b == original[i].b);
    CHECK(b.tracks[i].width == original[i].width);
    CHECK(b.tracks[i].net == original[i].net);
    CHECK(b.tracks[i].locked);
  }
}

TEST_CASE("access graph hard source keepouts reject before generating candidates", "[escape][access][budget]") {
  auto b = access_board();
  const auto rules = access_rules();
  SECTION("single copper source layer") {}
  SECTION("every copper source layer") {
    b.pads[0].copper |= model::layer_bit(1);
  }
  track_keepout(b, model::layer_bit(0) | model::layer_bit(1),
                -7'500'000, -7'500'000, 7'500'000, 7'500'000);
  route::Obstacles obs(b, rules);
  for (bool reference : {false, true}) {
    for (bool allow_vias : {false, true}) {
      CAPTURE(reference, allow_vias);
      route::AccessSearchOptions options;
      options.reference = reference;
      options.routing.allow_vias = allow_vias;
      options.record_candidates = true;
      options.work_budget = 64;
      const auto complete = route::generate_access_paths(b, rules, obs, 0, options);
      REQUIRE(complete.exhausted);
      CHECK(complete.paths.empty());
      CHECK(complete.candidates.empty());
      CHECK(complete.generation_work == 0);
      CHECK(complete.neighbor_work == 0);
      CHECK(complete.expansion_work == 0);
      CHECK(complete.check_work > 0);
      check_access_work(complete, options.work_budget);
      REQUIRE(complete.work > 1);
      // Even a cheap geometric proof must not claim exhaustion if its predicates were interrupted.
      options.work_budget = complete.work - 1;
      const auto interrupted = route::generate_access_paths(b, rules, obs, 0, options);
      CHECK_FALSE(interrupted.exhausted);
      CHECK(interrupted.paths.empty());
      CHECK(interrupted.candidates.empty());
      check_access_work(interrupted, options.work_budget);
      options.work_budget = complete.work;
      const auto exact_cap = route::generate_access_paths(b, rules, obs, 0, options);
      CHECK(exact_cap.exhausted);
      CHECK(exact_cap.work == complete.work);
      check_access_work(exact_cap, options.work_budget);
    }
  }
}

TEST_CASE("access graph searches an uncovered source layer instead of declaring the pad dead", "[escape][access]") {
  auto b = access_board();
  const auto rules = access_rules();
  b.pads[0].copper |= model::layer_bit(1);
  for (std::size_t i = 1; i < b.pads.size(); ++i) b.pads[i].pos.x += 20'000'000;
  track_keepout(b, model::layer_bit(0), -7'500'000, -7'500'000, 7'500'000, 7'500'000);
  route::Obstacles obs(b, rules);
  route::AccessSearchOptions options;
  options.work_budget = equivalence_work;
  options.record_candidates = true;
  options.routing.allow_vias = false;
  options.target = [](Point p, int layer, const std::function<bool()>&) {
    return layer == 1 && p.x >= 600'000 ? route::AccessGoal::Endpoint : route::AccessGoal::None;
  };
  const auto indexed = route::generate_access_paths(b, rules, obs, 0, options);
  REQUIRE_FALSE(indexed.paths.empty());
  CHECK(indexed.generation_work > 0);
  for (const auto& path : indexed.paths) {
    check_access_witness(b, obs, 0, path);
    CHECK(options.target(path.end, path.layer, [] { return true; }) != route::AccessGoal::None);
    CHECK(path.vias.empty());
    for (const auto& step : path.steps) CHECK(step.layer == 1);
  }
  options.reference = true;
  check_same_access(indexed, route::generate_access_paths(b, rules, obs, 0, options));
}

TEST_CASE("access graph permits an exact source via through a track-forbidden pad layer", "[escape][access]") {
  auto b = access_board();
  auto rules = access_rules();
  for (std::size_t i = 1; i < b.pads.size(); ++i) b.pads[i].pos.x += 20'000'000;
  SECTION("a saved rule area forbids tracks but permits vias") {
    track_keepout(b, model::layer_bit(0), -7'500'000, -7'500'000, 7'500'000, 7'500'000);
    b.zones.back().keepout_vias = false;
  }
  SECTION("a static custom rule forbids tracks but permits vias") {
    model::CustomRule rule;
    rule.name = "source layer tracks forbidden";
    rule.layer = "F.Cu";
    model::Constraint constraint;
    constraint.type = "disallow";
    constraint.items = {"track"};
    rule.constraints.push_back(constraint);
    rules.custom.push_back(rule);
  }
  SECTION("only a legal blind source via can bypass the forbidden layer") {
    b.layers = {{0, "F.Cu", "F.Cu", "signal", "", 0},
                {1, "In1.Cu", "In1.Cu", "signal", "", 1},
                {2, "In2.Cu", "In2.Cu", "signal", "", 2},
                {31, "B.Cu", "B.Cu", "signal", "", 3}};
    b.copper = {0, 1, 2, 3};
    rules.minimums.allow_blind_buried_vias = true;
    track_keepout(b, model::layer_bit(0), -7'500'000, -7'500'000, 7'500'000, 7'500'000);
    b.zones.back().keepout_vias = false;
    track_keepout(b, model::layer_bit(3), -7'500'000, -7'500'000, 7'500'000, 7'500'000);
  }
  route::Obstacles obs(b, rules);
  REQUIRE(obs.segment_state(b.pads[0].pos, {600'000, 0}, 0, 150'000, 1, false) == 2);
  if (b.copper_count() == 2) {
    REQUIRE(obs.via_state(b.pads[0].pos, 450'000, 300'000, 1, 0, false) == 0);
  } else {
    REQUIRE(obs.via_state(b.pads[0].pos, 450'000, 300'000, 1, 0, false) == 2);
    REQUIRE(obs.via_state_span(b.pads[0].pos, 450'000, 300'000, 1, 0, false, nullptr, 0, 1) == 0);
  }
  route::AccessSearchOptions options;
  options.work_budget = equivalence_work;
  options.record_candidates = true;
  options.target = [](Point p, int layer, const std::function<bool()>&) {
    return layer == 1 && p.x >= 600'000 ? route::AccessGoal::Endpoint : route::AccessGoal::None;
  };
  for (int mode : {0, 1, 2}) {
    if (mode == 2 && !rules.minimums.allow_blind_buried_vias) continue;
    CAPTURE(mode);
    options.routing.allow_vias = mode == 1;
    options.routing.blind_vias = mode > 0 && rules.minimums.allow_blind_buried_vias;
    options.reference = false;
    const auto indexed = route::generate_access_paths(b, rules, obs, 0, options);
    if (mode > 0) {
      REQUIRE_FALSE(indexed.paths.empty());
      for (const auto& path : indexed.paths) {
        check_access_witness(b, obs, 0, path);
        REQUIRE_FALSE(path.vias.empty());
        CHECK(path.vias.front().pos == b.pads[0].pos);
        if (b.copper_count() > 2) CHECK(path.vias.front().type == model::ViaType::Blind);
        for (const auto& step : path.steps) CHECK(step.layer == 1);
        CHECK(options.target(path.end, path.layer, [] { return true; }) != route::AccessGoal::None);
      }
    } else {
      CHECK(indexed.exhausted);
      CHECK(indexed.paths.empty());
      CHECK(indexed.candidates.empty());
      CHECK(indexed.work <= 64);
    }
    options.reference = true;
    check_same_access(indexed, route::generate_access_paths(b, rules, obs, 0, options));
  }
}

TEST_CASE("access graph source rejection uses exact keepout geometry, not its bounding box", "[escape][access]") {
  auto b = access_board();
  const auto rules = access_rules();
  for (std::size_t i = 1; i < b.pads.size(); ++i) b.pads[i].pos.x += 20'000'000;
  track_keepout(b, model::layer_bit(0), -1'000'000, -1'000'000, 1'000'000, 1'000'000);
  // The origin is inside the triangle's bounding box but outside its actual copper prohibition.
  b.zones.back().outline.front() = {{-1'000'000, 1'000'000}, {1'000'000, 1'000'000}, {1'000'000, -200'000}};
  route::Obstacles obs(b, rules);
  REQUIRE(obs.segment_state({0, 0}, {-600'000, 0}, 0, 150'000, 1, false) == 0);
  route::AccessSearchOptions options;
  options.work_budget = equivalence_work;
  options.record_candidates = true;
  options.routing.allow_vias = false;
  options.target = [](Point p, int layer, const std::function<bool()>&) {
    return layer == 0 && p.x <= -600'000 ? route::AccessGoal::Endpoint : route::AccessGoal::None;
  };
  const auto indexed = route::generate_access_paths(b, rules, obs, 0, options);
  REQUIRE_FALSE(indexed.paths.empty());
  for (const auto& path : indexed.paths) check_access_witness(b, obs, 0, path);
  options.reference = true;
  check_same_access(indexed, route::generate_access_paths(b, rules, obs, 0, options));
}

TEST_CASE("access graph caps generation, neighbors, predicates and expansion without a false proof", "[escape][access][budget]") {
  auto b = access_board();
  const auto rules = access_rules();
  for (std::size_t i = 1; i < b.pads.size(); ++i) b.pads[i].pos.x += 20'000'000;
  route::Obstacles obs(b, rules);
  route::AccessSearchOptions options;
  options.radius = 600'000;
  options.routing.allow_vias = false;
  options.record_candidates = true;
  options.target = [](Point, int, const std::function<bool()>&) { return route::AccessGoal::None; };  // exhaust the finite domain, not the max-path limit
  route::AccessSearchResult indexed;
  for (bool reference : {false, true}) {
    CAPTURE(reference);
    options.reference = reference;
    options.work_budget = equivalence_work;
    const auto complete = route::generate_access_paths(b, rules, obs, 0, options);
    REQUIRE(complete.exhausted);
    CHECK(complete.paths.empty());
    REQUIRE_FALSE(complete.candidates.empty());
    // Lower bounds count known ray/pad generation and one rule/exact predicate per candidate layer;
    // counter self-consistency alone would miss work silently omitted from every counter.
    CHECK(complete.generation_work >= 8 * (options.radius / 100'000) + static_cast<long>(b.pads.size()));
    CHECK(complete.neighbor_work >= static_cast<long>(complete.candidates.size()));
    CHECK(complete.check_work >= static_cast<long>(complete.candidates.size()) * b.copper_count());
    CHECK(complete.expansion_work > 0);
    check_access_work(complete, options.work_budget);
    REQUIRE(complete.work > 64);
    if (reference) check_same_access(indexed, complete);
    else indexed = complete;
    for (long cap : {1L, 2L, 16L, 64L, complete.work - 1}) {
      CAPTURE(cap);
      options.work_budget = cap;
      const auto limited = route::generate_access_paths(b, rules, obs, 0, options);
      CHECK_FALSE(limited.exhausted);
      CHECK(limited.paths.empty());
      CHECK(limited.work == cap);
      check_access_work(limited, cap);
      if (cap <= 16) CHECK(limited.candidates.empty());
    }
    options.work_budget = complete.work;
    const auto exact_cap = route::generate_access_paths(b, rules, obs, 0, options);
    REQUIRE(exact_cap.exhausted);
    check_access_work(exact_cap, options.work_budget);
    check_same_access(complete, exact_cap);
  }
}

TEST_CASE("router access preparation cannot starve an unrelated route behind hard-boxed pads", "[escape][access][budget][route]") {
  auto b = access_board();
  const auto rules = access_rules();
  route::RouterOptions options;
  // Nine impossible dense-package obligations are deliberately scheduled before the ordinary connection.
  for (std::size_t i = 0; i < 9; ++i) {
    b.pads[i].net = static_cast<model::NetId>(i + 1);
    auto terminal = b.pads[i];
    terminal.footprint = 1;
    terminal.pos = {10'000'000, static_cast<Coord>(i) * 600'000};
    if (i == 0) b.pads[9] = terminal;
    else {
      b.footprints[1].pads.push_back(static_cast<int>(b.pads.size()));
      b.pads.push_back(terminal);
    }
    options.priority.emplace_back("U1." + terminal.number, "J1." + terminal.number);
  }
  model::Net free_net;
  free_net.id = static_cast<model::NetId>(b.nets.size());
  free_net.name = "unrelated";
  b.nets.push_back(free_net);
  b.footprints.push_back({});
  b.footprints.back().reference = "J2";
  for (int i = 0; i < 2; ++i) {
    auto pad = b.pads[0];
    pad.footprint = 2;
    pad.net = free_net.id;
    pad.number = std::to_string(i + 1);
    pad.pos = {static_cast<Coord>(i) * 2'000'000, 10'000'000};
    b.footprints.back().pads.push_back(static_cast<int>(b.pads.size()));
    b.pads.push_back(pad);
  }
  track_keepout(b, model::layer_bit(0) | model::layer_bit(1),
                -7'500'000, -7'500'000, 7'500'000, 7'500'000);
  route::Obstacles obs(b, rules);
  options.work_budget = 50'000;
  options.pitch = 100'000;
  options.max_expansions = 1'000;
  options.max_attempts = 1;
  options.max_passes = 2;
  options.max_restarts = 0;
  options.allow_vias = false;
  options.rip_up = false;
  options.optimize = false;
  options.gpu_device = -1;
  options.time_limit_s = 600;
  const auto result = route::Router(b, rules, options).run();
  REQUIRE(result.connections == 10);
  REQUIRE(result.routed == 1);
  REQUIRE_FALSE(result.tracks.empty());
  CHECK(result.vias.empty());
  CHECK(result.access_work > 0);
  CHECK(result.access_lattice_work > 0);
  check_router_access_work(result, options.work_budget);
  for (const auto& track : result.tracks) {
    CHECK(track.net == free_net.id);
    CHECK(obs.segment_state(track.a, track.b, track.layer, track.width, track.net, false) == 0);
  }
  REQUIRE(result.unrouted.size() == 9);
  for (const auto& pending : result.unrouted) CHECK(pending.net != free_net.name);
  CHECK(b.tracks.empty());
  CHECK(b.vias.empty());
}

// ---- Escape planning v2: min-cost-flow channel and layer assignment (route/escape_flow.hpp) ----

namespace {

route::FlowEscapeInput flow_input(Coord w, Coord s, Coord via, int layers) {
  route::FlowEscapeInput in;
  in.width = [w](model::NetId) { return w; };
  in.clearance = [s](model::NetId) { return s; };
  in.via = [via](model::NetId) { return via; };
  in.keep = [w, s](model::NetId) { return w + s; };
  in.layers = layers;
  return in;
}

// The corridor's polyline on the layer it ends on (the tail, preceded by b), or on the pad layer when it has none.
std::vector<Point> tail_of(const route::EscapeCorridor& c) {
  std::vector<Point> pts{c.b};
  pts.insert(pts.end(), c.tail.begin(), c.tail.end());
  if (!c.via) pts.insert(pts.begin(), c.a);
  return pts;
}

// Does the polyline cross the open middle of segment p-q (between 20 % and 80 % of its length)?
bool crosses_middle(const std::vector<Point>& pl, Point p, Point q) {
  for (std::size_t k = 0; k + 1 < pl.size(); ++k) {
    const double ax = static_cast<double>(pl[k].x), ay = static_cast<double>(pl[k].y);
    const double bx = static_cast<double>(pl[k + 1].x), by = static_cast<double>(pl[k + 1].y);
    const double px = static_cast<double>(p.x), py = static_cast<double>(p.y), qx = static_cast<double>(q.x), qy = static_cast<double>(q.y);
    const double d = (bx - ax) * (qy - py) - (by - ay) * (qx - px);
    if (std::fabs(d) < 1e-9) continue;
    const double t = ((px - ax) * (qy - py) - (py - ay) * (qx - px)) / d;   // along the polyline segment
    const double u = ((px - ax) * (by - ay) - (py - ay) * (bx - ax)) / d;   // along p-q
    if (t >= -1e-9 && t <= 1 + 1e-9 && u >= 0.2 && u <= 0.8) return true;
  }
  return false;
}

}  // namespace

TEST_CASE("escape flow: a 6 x 6 array escapes on its pad layer within every channel's capacity", "[escape]") {
  const Coord pitch = 1'000'000, ball = 400'000, w = 150'000, s = 150'000;
  const auto b = bga(6, pitch, ball);
  std::vector<char> needs(b.pads.size(), 1);
  route::FlowEscapeStats st;
  const auto plan = route::plan_escapes_flow(b, needs, flow_input(w, s, 600'000, 1), {}, &st);
  CHECK(st.arrays == 1);
  REQUIRE(st.rings.size() == 3);
  CHECK(st.rings[0].pins == 20);
  CHECK(st.rings[1].pins == 12);
  CHECK(st.rings[2].pins == 4);
  // One track fits between two balls (0.6 mm gap, 0.15 / 0.15 mm rules): 20 boundary channels for 16 inner balls.
  for (const auto& r : st.rings) CHECK(r.pad_layer == r.pins);
  REQUIRE(plan.size() == 36);
  // Independent check of the plan's geometry: no more corridors between two neighbouring balls than fit there
  // (one), none through a gap's centre more often than its diagonal allows (two), and every corridor keeps the
  // track clear of the other balls.
  auto pos = [&](int i, int j) { return Point{i * pitch, j * pitch}; };
  for (int j = 0; j < 6; ++j)
    for (int i = 0; i < 6; ++i)
      for (const auto [di, dj] : {std::pair{1, 0}, std::pair{0, 1}}) {
        if (i + di >= 6 || j + dj >= 6) continue;
        int n = 0;
        for (const auto& c : plan) n += crosses_middle(tail_of(c), pos(i, j), pos(i + di, j + dj));
        CHECK(n <= 1);
      }
  for (int j = 0; j < 5; ++j)
    for (int i = 0; i < 5; ++i) {
      const Point centre{i * pitch + pitch / 2, j * pitch + pitch / 2};
      int n = 0;
      for (const auto& c : plan) n += static_cast<int>(std::count(c.tail.begin(), c.tail.end(), centre)) + (c.b == centre);
      CHECK(n <= 2);
    }
  for (const auto& c : plan) {
    const auto pl = tail_of(c);
    for (const auto& p : b.pads) {
      if (p.pos == b.pads[static_cast<std::size_t>(c.pad)].pos) continue;
      for (std::size_t k = 0; k + 1 < pl.size(); ++k) CHECK(dist_point_segment(p.pos, pl[k], pl[k + 1]) >= static_cast<double>(ball / 2 + s + w / 2) - 1);
    }
    // ends outside the array
    const Point e = pl.back();
    CHECK((e.x < 0 || e.y < 0 || e.x > 5 * pitch || e.y > 5 * pitch));
  }
  // Deterministic.
  const auto again = route::plan_escapes_flow(b, needs, flow_input(w, s, 600'000, 1));
  REQUIRE(again.size() == plan.size());
  for (std::size_t i = 0; i < plan.size(); ++i) CHECK((again[i].pad == plan[i].pad && again[i].b == plan[i].b && again[i].tail == plan[i].tail));
}

TEST_CASE("escape flow: without channels between balls, inner rings take dog-bone vias and leave on the next layer", "[escape]") {
  // 0.8 mm pitch, 0.45 mm balls, 0.1 / 0.15 mm rules: 0.35 mm between balls is too narrow for one track.
  const Coord pitch = 800'000, ball = 450'000, w = 100'000, s = 150'000, via = 300'000;
  const auto b = bga(8, pitch, ball);
  std::vector<char> needs(b.pads.size(), 1);
  route::FlowEscapeStats st;
  const auto plan = route::plan_escapes_flow(b, needs, flow_input(w, s, via, 4), {}, &st);
  REQUIRE(st.rings.size() == 4);
  CHECK(st.rings[0].pad_layer == 28);  // the perimeter leaves on its own layer
  CHECK(st.rings[1].pad_layer == 0);
  int via_pins = 0, other = 0;
  for (const auto& r : st.rings) via_pins += r.other_layer + r.via_only, other += r.other_layer;
  CHECK(via_pins == 36);  // every inner ball gets a via site
  CHECK(other > 0);
  CHECK(st.per_layer[0] == 28);
  CHECK(st.per_layer[1] > 0);  // the nearest layer first
  std::set<std::pair<Coord, Coord>> sites;
  for (const auto& c : plan) {
    if (!c.via) continue;
    CHECK(sites.insert({c.b.x, c.b.y}).second);  // one via per interstitial site
    CHECK(std::llabs(std::llabs(c.b.x - c.a.x) - pitch / 2) <= 1);
    CHECK(std::llabs(std::llabs(c.b.y - c.a.y) - pitch / 2) <= 1);
    if (c.tail.empty()) continue;
    CHECK(c.tail_layer > 0);
    // On the via layers the vias are the obstacles: the tail keeps clear of every other planned via.
    for (const auto& o : plan)
      if (o.via && o.pad != c.pad)
        for (std::size_t k = 0; k + 1 < c.tail.size(); ++k)
          CHECK(dist_point_segment(o.b, c.tail[k], c.tail[k + 1]) >= static_cast<double>(via / 2 + s + w / 2) - 1);
  }
}

TEST_CASE("escape flow: shallow packages are planned exactly as version 1", "[escape]") {
  const auto b = bga(4, 800'000, 400'000);  // two rings only
  std::vector<char> needs(b.pads.size(), 1);
  const auto v1 = route::plan_escapes(b, needs, [](model::NetId) { return Coord{300'000}; });
  route::FlowEscapeStats st;
  const auto v2 = route::plan_escapes_flow(b, needs, flow_input(100'000, 100'000, 400'000, 2), {}, &st);
  CHECK(st.arrays == 0);
  REQUIRE(v1.size() == v2.size());
  for (std::size_t i = 0; i < v1.size(); ++i) CHECK((v1[i].pad == v2[i].pad && v1[i].b == v2[i].b && v1[i].via == v2[i].via && v2[i].tail.empty()));
  CHECK(route::array_rings(b, needs) == std::vector<int>(b.pads.size(), 0));
  const auto deep = bga(7, 800'000, 400'000);
  std::vector<char> all(deep.pads.size(), 1);
  const auto rings = route::array_rings(deep, all);
  CHECK(rings[0] == 1);
  CHECK(rings[static_cast<std::size_t>(3 * 7 + 3)] == 4);
}

TEST_CASE("blind-only access does not fabricate a full-stack through via", "[escape][access][via]") {
  auto b = access_board();
  auto rules = access_rules();
  b.layers = {{0, "F.Cu", "F.Cu", "signal", "", 0}, {1, "In1.Cu", "In1.Cu", "signal", "", 1},
              {2, "In2.Cu", "In2.Cu", "signal", "", 2}, {31, "B.Cu", "B.Cu", "signal", "", 3}};
  b.copper = {0, 1, 2, 3};
  rules.minimums.allow_blind_buried_vias = true;
  track_keepout(b, model::layer_bit(0) | model::layer_bit(1) | model::layer_bit(2),
                -7'500'000, -7'500'000, 7'500'000, 7'500'000);
  b.zones.back().keepout_vias = false;
  route::Obstacles obs(b, rules);
  route::AccessSearchOptions options;
  options.routing.allow_vias = false;
  options.routing.blind_vias = true;
  for (bool reference : {false, true}) {
    options.reference = reference;
    const auto result = route::generate_access_paths(b, rules, obs, 0, options);
    CHECK(result.paths.empty());
    CHECK(result.exhausted);
    check_access_work(result, options.work_budget);
  }
}
