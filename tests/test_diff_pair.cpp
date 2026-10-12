// SPDX-License-Identifier: GPL-3.0-or-later
// Differential pairs (M12, doc 05 §15): the pair rule (gap and width from net class, custom rules and the clearance
// KiCad requires between the halves), offset/miter geometry, and the coupled-share / gap / skew measurement.
#include <catch2/catch_test_macros.hpp>

#include <cmath>

#include "drc/copper.hpp"
#include "drc/drc.hpp"
#include "drc/rule_engine.hpp"
#include "io/kicad/board_reader.hpp"
#include "route/diff_pair.hpp"
#include "route/router.hpp"

using namespace tmk;
using geom::Point;

namespace {

model::Board two_nets(const std::string& a, const std::string& b) {
  model::Board bd;
  bd.nets.push_back({});
  model::Net n;
  n.id = 1;
  n.name = a;
  bd.nets.push_back(n);
  n.id = 2;
  n.name = b;
  bd.nets.push_back(n);
  return bd;
}

model::DesignRules rules_with(Coord clearance, bool dp, Coord dp_width, Coord dp_gap) {
  model::DesignRules r;
  model::NetClass k;
  k.name = "Default";
  k.clearance = clearance;
  k.track_width = 250'000;
  k.has_diff_pair_gap = dp;
  k.diff_pair_width = dp_width;
  k.diff_pair_gap = dp_gap;
  r.classes.push_back(k);
  return r;
}

double dist(Point a, Point b) { return std::hypot(static_cast<double>(a.x - b.x), static_cast<double>(a.y - b.y)); }

}  // namespace

TEST_CASE("pair rule: tightest legal coupling without diff-pair settings", "[diffpair]") {
  const auto b = two_nets("USB_DP", "USB_DM");  // not a KiCad pair by name: the full clearance applies
  const auto r = rules_with(200'000, false, 0, 0);
  const drc::RuleEngine re(b, r);
  const auto pr = route::pair_rule(b, r, re, 1, 2);
  CHECK(pr.width == 250'000);
  CHECK(pr.gap == 200'000);
  CHECK(pr.required_gap == 200'000);
  CHECK(pr.source == "clearance");
  CHECK(2 * pr.offset - pr.width >= pr.gap);           // offsets never undercut the gap
  CHECK(2 * pr.offset - pr.width <= pr.gap + 3'000);   // and add only the rounding margin
  CHECK(2 * pr.via_offset - pr.via_diameter >= pr.required_gap);
}

TEST_CASE("pair rule: net-class diff-pair width and gap, relaxed clearance only for KiCad pairs", "[diffpair]") {
  const auto r = rules_with(200'000, true, 180'000, 150'000);
  {
    const auto b = two_nets("/TMDS_0_P", "/TMDS_0_N");
    const drc::RuleEngine re(b, r);
    const auto pr = route::pair_rule(b, r, re, 1, 2);
    CHECK(pr.width == 180'000);
    CHECK(pr.gap == 150'000);  // KiCad relaxes the clearance between the halves to the diff-pair gap
    CHECK(pr.source == "net class");
  }
  {
    const auto b = two_nets("DP", "DM");
    const drc::RuleEngine re(b, r);
    const auto pr = route::pair_rule(b, r, re, 1, 2);
    CHECK(pr.gap == 200'000);  // not a pair to KiCad: the gap may not undercut the clearance
  }
}

TEST_CASE("pair rule: a custom diff_pair_gap rule wins, diff_pair_uncoupled limits the legs", "[diffpair]") {
  auto r = rules_with(150'000, false, 0, 0);
  model::CustomRule cr;
  cr.name = "pairs";
  model::Constraint g;
  g.type = "diff_pair_gap";
  g.opt = 300'000;
  model::Constraint u;
  u.type = "diff_pair_uncoupled";
  u.max = 4'000'000;
  cr.constraints = {g, u};
  r.custom.push_back(cr);
  const auto b = two_nets("D+", "D-");
  const drc::RuleEngine re(b, r);
  const auto pr = route::pair_rule(b, r, re, 1, 2);
  CHECK(pr.gap == 300'000);
  CHECK(pr.source == "rule");
  REQUIRE(pr.max_uncoupled);
  CHECK(*pr.max_uncoupled == 4'000'000);
}

TEST_CASE("pair geometry: offsets are symmetric and miters keep the offset distance", "[diffpair]") {
  const Point c{1'000'000, 2'000'000};
  for (int d = 0; d < 8; ++d) {
    const Point l = route::offset_point(c, route::left_normal(d), 300'000), r = route::offset_point(c, route::left_normal(d), -300'000);
    CHECK(std::fabs(dist(l, r) - 600'000) <= 2);
    CHECK(l.x - c.x == c.x - r.x);
    CHECK(l.y - c.y == c.y - r.y);
    for (int t : {1, 7}) {
      const int d2 = (d + t) & 7;
      const Point m = route::miter_point(c, d, d2, 300'000);
      // The corner lies on both offset lines: its distance to each centreline direction's line is the offset.
      for (int dd : {d, d2}) {
        const auto u = route::unit_dir(dd);
        const double cross = static_cast<double>(m.x - c.x) * u.y - static_cast<double>(m.y - c.y) * u.x;
        CHECK(std::fabs(std::fabs(cross) - 300'000) <= 2);
      }
    }
  }
}

TEST_CASE("pair pad exits follow rotated copper axes and offsets", "[diffpair]") {
  model::Pad p;
  p.pos = {2'000'000, 3'000'000};
  p.size_x = 300'000;
  p.size_y = 1'500'000;
  p.drill_offset = {100'000, 0};
  p.angle = 90;
  const auto exits = route::pair_pad_exits(p, 200'000);
  const Point centre = p.pos + geom::rotate(p.drill_offset, p.angle);
  CHECK(dist(exits[0], centre) == 350'000);
  CHECK(dist(exits[1], centre) == 350'000);
  CHECK(dist(exits[2], centre) == 950'000);
  CHECK(dist(exits[3], centre) == 950'000);
  CHECK(exits[0].x == centre.x);
  CHECK(exits[2].y == centre.y);
}

TEST_CASE("pair measurement: coupled share, gap and skew", "[diffpair]") {
  std::vector<model::Track> t;
  // Two parallel 10 mm tracks, 0.25 mm wide, edges 0.2 mm apart, then net 1 runs on alone for 2 mm.
  t.push_back({{0, 0}, {10'000'000, 0}, 250'000, 0, 1, false, sexpr::kNoNode});
  t.push_back({{0, 450'000}, {10'000'000, 450'000}, 250'000, 0, 2, false, sexpr::kNoNode});
  t.push_back({{10'000'000, 0}, {12'000'000, 0}, 250'000, 0, 1, false, sexpr::kNoNode});
  // A crossing track of net 2 on the other layer does not count.
  t.push_back({{5'000'000, -1'000'000}, {5'000'000, 1'000'000}, 250'000, 1, 2, false, sexpr::kNoNode});
  const auto st = route::measure_pair(t, {}, 1, 2, 260'000);
  CHECK(std::fabs(st.length_a - 12e6) < 1);
  CHECK(std::fabs(st.length_b - 12e6) < 1);
  // Distance is to the partner segment, its ends included: a half running on past the partner's end still counts
  // while within the threshold of that end (here 0.24 mm), which keeps miter corners from counting as uncoupled.
  CHECK(st.coupled_a >= 10e6 - 1e4);
  CHECK(st.coupled_a <= 10.3e6);
  CHECK(std::fabs(st.coupled_b - 10e6) < 1e4);
  CHECK(std::fabs(st.gap_median - 200'000) < 1);
  CHECK(std::fabs(st.gap_min - 200'000) < 1);
  CHECK(st.coupled_share() >= 20.0 / 24.0 - 1e-3);
  CHECK(st.coupled_share() <= 20.3 / 24.0);
  CHECK(st.skew() < 1);
  // Farther apart than the threshold: nothing coupled.
  const auto far = route::measure_pair(t, {}, 1, 2, 150'000);
  CHECK(far.coupled_a == 0);
  CHECK(far.gap_median == 0);
}

namespace {

// 40 x 20 mm, one copper layer (no via can hop the fence). Pair D_P/D_N runs from U1 (x = 5) to U2 (x = 35); a locked
// ring of net W surrounds both pads of each end, so no pair or single route gets out.
model::Board fenced_pair() {
  std::string s = R"((kicad_pcb (version 20240108) (generator "pcbnew")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "") (net 1 "D_P") (net 2 "D_N") (net 3 "W")
)";
  for (const int x : {5, 35}) {
    const std::string ref = x == 5 ? "U1" : "U2", at = std::to_string(x);
    s += "  (footprint \"T:P\" (layer \"F.Cu\") (at " + at + " 10) (property \"Reference\" \"" + ref + "\")\n"
         "    (pad \"1\" smd rect (at 0 -0.3) (size 0.3 0.3) (layers \"F.Cu\") (net 1 \"D_P\"))\n"
         "    (pad \"2\" smd rect (at 0 0.3) (size 0.3 0.3) (layers \"F.Cu\") (net 2 \"D_N\")))\n";
    const double x0 = x - 0.75, x1 = x + 0.75;
    for (const auto& [a, b] : {std::pair{std::pair{x0, 9.0}, std::pair{x1, 9.0}}, std::pair{std::pair{x1, 9.0}, std::pair{x1, 11.0}},
                               std::pair{std::pair{x1, 11.0}, std::pair{x0, 11.0}}, std::pair{std::pair{x0, 11.0}, std::pair{x0, 9.0}}})
      s += "  (segment (start " + std::to_string(a.first) + " " + std::to_string(a.second) + ") (end " + std::to_string(b.first) + " " +
           std::to_string(b.second) + ") (width 0.2) (locked yes) (layer \"F.Cu\") (net 3))\n";
  }
  s += "  (gr_rect (start 0 0) (end 40 20) (stroke (width 0.1) (type default)) (fill none) (layer \"Edge.Cuts\"))\n)\n";
  return io::read_board(sexpr::Document::parse(s));
}

route::RouteResult route_fenced(const model::Board& b, long work) {
  route::RouterOptions o;
  o.work_budget = work;
  o.time_limit_s = 600;
  o.gpu_device = -1;
  o.pair_nets = {{b.net_by_name("D_P"), b.net_by_name("D_N")}};
  return route::Router(b, rules_with(200'000, false, 0, 0), o).run();
}

}  // namespace

TEST_CASE("pair search: rejected candidates are work, and the work budget stops an attempt", "[diffpair][route]") {
  const auto b = fenced_pair();
  const auto full = route_fenced(b, 0);
  CHECK(full.routed == 0);
  // Closed states are few (no leg is legal, so no state is ever reached); the charge is the leg checks of the
  // thousands of rejected candidates. Charging only closed states left such attempts at about zero work.
  CHECK(full.pair_work > 5'000);
  CHECK(full.expansions >= full.pair_work);
  // Poll inside each nested leg check; the outer queue polls at most 64 states/candidates apart.
  const long budget = 2'000;
  const auto cut = route_fenced(b, budget);
  CHECK(cut.pair_work < full.pair_work);
  CHECK(cut.pair_work <= budget / 10 + 64);
  const auto again = route_fenced(b, budget);
  CHECK(again.expansions == cut.expansions);
  CHECK(again.pair_work == cut.pair_work);
}

TEST_CASE("upfront pair probes reserve fixed work for ordinary connections", "[diffpair][route]") {
  auto b = fenced_pair();
  model::Net signal;
  signal.id = 4;
  signal.name = "SIGNAL";
  b.nets.push_back(signal);
  for (const Coord x : {10'000'000, 11'000'000}) {
    auto p = b.pads.front();
    p.net = signal.id;
    p.pos = {x, 3'000'000};
    p.footprint = -1;
    p.node = sexpr::kNoNode;
    b.pads.push_back(p);
  }
  route::RouterOptions o;
  o.diff_pairs = true;
  o.allow_vias = false;
  o.gpu_device = -1;
  o.optimize = false;
  o.max_passes = 1;
  o.rip_up = false;
  o.max_restarts = 0;
  o.work_budget = 100'000;
  const auto r = rules_with(200'000, false, 0, 0);
  const auto first = route::Router(b, r, o).run(), second = route::Router(b, r, o).run();
  REQUIRE(first.connections == 3);
  CHECK(first.routed == 1);
  CHECK(first.pair_work <= o.work_budget / 10 + 64);
  CHECK(first.stop != "work");
  CHECK(first.expansions == second.expansions);
  CHECK(first.pair_work == second.pair_work);
  REQUIRE(first.tracks.size() == second.tracks.size());
  for (std::size_t i = 0; i < first.tracks.size(); ++i) {
    CHECK(first.tracks[i].net == signal.id);
    CHECK(first.tracks[i].a == second.tracks[i].a);
    CHECK(first.tracks[i].b == second.tracks[i].b);
    CHECK(first.tracks[i].width == second.tracks[i].width);
    CHECK(first.tracks[i].layer == second.tracks[i].layer);
  }
  b.tracks.insert(b.tracks.end(), first.tracks.begin(), first.tracks.end());
  const auto report = drc::run_drc(b, r);
  for (const auto& v : report.unconnected) CHECK(v.description.find("SIGNAL") == std::string::npos);
  for (const auto& v : report.violations) {
    CHECK(v.type != "clearance");
    CHECK(v.type != "shorting_items");
  }
}

TEST_CASE("pair cleanup preserves three-terminal tree connectivity", "[diffpair][route][tree]") {
  auto b = fenced_pair();
  b.tracks.clear();
  for (auto& p : b.pads)
    if (p.pos.x > 20'000'000) p.pos.x = 15'000'000;
  b.footprints[1].pos.x = 15'000'000;
  // Both nets have a terminal beside the trunk, in a separate package: ordinary routing can attach
  // there rather than duplicate the pad-to-pad trunk. The pair cleanup must not move its anchor.
  for (const model::NetId net : {1, 2}) {
    auto p = b.pads[static_cast<std::size_t>(net - 1)];
    p.pos = {10'000'000, net == 1 ? 17'000'000 : 17'600'000};
    model::Footprint fp;
    fp.reference = net == 1 ? "U3" : "U4";
    fp.pos = p.pos;
    fp.pads = {static_cast<int>(b.pads.size())};
    p.footprint = static_cast<int>(b.footprints.size());
    p.node = sexpr::kNoNode;
    b.footprints.push_back(std::move(fp));
    b.pads.push_back(p);
  }
  route::RouterOptions o;
  o.diff_pairs = true;
  o.tree_attach = true;
  o.pair_max_expansions = 8;
  o.allow_vias = false;
  o.gpu_device = -1;
  o.work_budget = 2'000'000;
  o.time_limit_s = 600;
  auto r = rules_with(200'000, false, 0, 0);
  model::CustomRule limit;
  limit.name = "force single routing before cleanup";
  model::Constraint uncoupled;
  uncoupled.type = "diff_pair_uncoupled";
  uncoupled.max = 1;
  limit.constraints.push_back(uncoupled);
  r.custom.push_back(limit);
  const auto result = route::Router(b, r, o).run();
  REQUIRE(result.routed == result.connections);
  REQUIRE(result.pair_anchor_skips > 0);
  CHECK(result.pairs == 0);
  bool restored_attempt = false;
  for (const auto& attempt : result.pair_attempts) restored_attempt |= attempt.cleanup && !attempt.coupled;
  CHECK(restored_attempt);
  o.pair_max_expansions = 1;
  const auto untouched = route::Router(b, r, o).run();
  // Two different failed search depths must restore byte-identical physical copper.
  // Keep pair selection enabled so only the failed-search depth differs.
  REQUIRE(result.tracks.size() == untouched.tracks.size());
  REQUIRE(result.vias.size() == untouched.vias.size());
  for (std::size_t i = 0; i < result.tracks.size(); ++i) {
    CHECK(result.tracks[i].a == untouched.tracks[i].a);
    CHECK(result.tracks[i].b == untouched.tracks[i].b);
    CHECK(result.tracks[i].width == untouched.tracks[i].width);
    CHECK(result.tracks[i].layer == untouched.tracks[i].layer);
    CHECK(result.tracks[i].net == untouched.tracks[i].net);
  }
  b.tracks.insert(b.tracks.end(), result.tracks.begin(), result.tracks.end());
  b.vias.insert(b.vias.end(), result.vias.begin(), result.vias.end());
  CHECK(drc::run_drc(b, r).unconnected.empty());
}

TEST_CASE("pair search couples adjacent fine-pitch rows with exact legal widths and gaps", "[diffpair][route]") {
  std::string text = R"((kicad_pcb (version 20240108) (generator "pcbnew")
    (general (thickness 1.6))
    (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
    (setup (pad_to_mask_clearance 0))
    (net 0 "") (net 1 "D_P") (net 2 "D_N") (net 3 "GND") (net 4 "E_P") (net 5 "E_N")
    (net 6 "F_P") (net 7 "F_N")
    (footprint "T:P" (layer "F.Cu") (at 5 5) (property "Reference" "J1")
      (pad "1" smd rect (at 0 -0.25) (size 1.5 0.3) (layers "F.Cu") (net 1 "D_P"))
      (pad "2" smd rect (at 0 0.25) (size 1.5 0.3) (layers "F.Cu") (net 2 "D_N"))
      (pad "3" smd rect (at 0 -0.75) (size 1.5 0.3) (layers "F.Cu") (net 3 "GND"))
      (pad "4" smd rect (at 0 0.75) (size 1.5 0.3) (layers "F.Cu") (net 3 "GND"))
      (pad "5" smd rect (at 0 1.25) (size 1.5 0.3) (layers "F.Cu") (net 4 "E_P"))
      (pad "6" smd rect (at 0 1.75) (size 1.5 0.3) (layers "F.Cu") (net 5 "E_N"))
      (pad "7" smd rect (at 0 2.25) (size 1.5 0.3) (layers "F.Cu") (net 3 "GND"))
      (pad "8" smd rect (at 0 2.75) (size 1.5 0.3) (layers "F.Cu") (net 6 "F_P"))
      (pad "9" smd rect (at 0 3.25) (size 1.5 0.3) (layers "F.Cu") (net 7 "F_N"))
      (pad "10" smd rect (at 0 3.75) (size 1.5 0.3) (layers "F.Cu") (net 3 "GND")))
    (footprint "T:P" (layer "B.Cu") (at 25 10) (property "Reference" "U1")
      (pad "1" smd rect (at -0.25 0) (size 0.3 1.5) (layers "B.Cu") (net 1 "D_P"))
      (pad "2" smd rect (at 0.25 0) (size 0.3 1.5) (layers "B.Cu") (net 2 "D_N"))
      (pad "3" smd rect (at -0.75 0) (size 0.3 1.5) (layers "B.Cu") (net 3 "GND"))
      (pad "4" smd rect (at 0.75 0) (size 0.3 1.5) (layers "B.Cu") (net 3 "GND"))
      (pad "5" smd rect (at 1.25 0) (size 0.3 1.5) (layers "B.Cu") (net 4 "E_P"))
      (pad "6" smd rect (at 1.75 0) (size 0.3 1.5) (layers "B.Cu") (net 5 "E_N"))
      (pad "7" smd rect (at 2.25 0) (size 0.3 1.5) (layers "B.Cu") (net 3 "GND"))
      (pad "8" smd rect (at 2.75 0) (size 0.3 1.5) (layers "B.Cu") (net 6 "F_P"))
      (pad "9" smd rect (at 3.25 0) (size 0.3 1.5) (layers "B.Cu") (net 7 "F_N"))
      (pad "10" smd rect (at 3.75 0) (size 0.3 1.5) (layers "B.Cu") (net 3 "GND")))
    (gr_rect (start 0 0) (end 30 15) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
  ))";
  const auto original = io::read_board(sexpr::Document::parse(text));
  auto r = rules_with(150'000, true, 150'000, 150'000);
  r.minimums.track_width = 150'000;
  model::CustomRule breakout;
  breakout.name = "short pair breakouts";
  model::Constraint uncoupled;
  uncoupled.type = "diff_pair_uncoupled";
  uncoupled.max = 4'000'000;
  breakout.constraints.push_back(uncoupled);
  r.custom.push_back(breakout);
  route::RouterOptions o;
  o.work_budget = 100'000'000;  // keep cached and exact-reference probes below their shared allowance
  o.time_limit_s = 600;
  o.gpu_device = -1;
  o.diff_pairs = true;
  for (const bool reversed : {false, true}) {
    auto b = original;
    if (reversed) {
      for (auto& p : b.pads) {
        p.pos = {30'000'000 - p.pos.x, 15'000'000 - p.pos.y};
        p.angle += 180;
      }
    }
    const auto result = route::Router(b, r, o).run();
    auto reference_options = o;
    reference_options.pair_leg_cache = false;
    const auto reference = route::Router(b, r, reference_options).run();
    REQUIRE(result.tracks.size() == reference.tracks.size());
    REQUIRE(result.vias.size() == reference.vias.size());
    CHECK(result.pair_work <= reference.pair_work);
    for (std::size_t i = 0; i < result.tracks.size(); ++i) {
      CHECK(result.tracks[i].a == reference.tracks[i].a);
      CHECK(result.tracks[i].b == reference.tracks[i].b);
      CHECK(result.tracks[i].width == reference.tracks[i].width);
      CHECK(result.tracks[i].layer == reference.tracks[i].layer);
      CHECK(result.tracks[i].net == reference.tracks[i].net);
    }
    for (std::size_t i = 0; i < result.vias.size(); ++i) {
      CHECK(result.vias[i].pos == reference.vias[i].pos);
      CHECK(result.vias[i].size == reference.vias[i].size);
      CHECK(result.vias[i].drill == reference.vias[i].drill);
      CHECK(result.vias[i].net == reference.vias[i].net);
      CHECK(result.vias[i].layer_top == reference.vias[i].layer_top);
      CHECK(result.vias[i].layer_bottom == reference.vias[i].layer_bottom);
      CHECK(result.vias[i].type == reference.vias[i].type);
    }
    REQUIRE(result.pairs >= 3);
    for (const auto& [a, partner] : {std::pair{1, 2}, std::pair{4, 5}, std::pair{6, 7}}) {
      const auto st = route::measure_pair(result.tracks, result.vias, a, partner, 200'000);
      CHECK(st.coupled_share() > 0.70);
    }
    for (const auto& t : result.tracks) CHECK(t.width >= r.minimums.track_width);
    b.tracks.insert(b.tracks.end(), result.tracks.begin(), result.tracks.end());
    b.vias.insert(b.vias.end(), result.vias.begin(), result.vias.end());
    for (const auto& v : drc::run_drc(b, r).violations) {
      INFO(v.description);
      CHECK(v.type != "clearance");
      CHECK(v.type != "shorting_items");
      CHECK(v.type != "hole_clearance");
    }
  }
}

TEST_CASE("pair search drains a finish reached at its local state cap deterministically", "[diffpair][route]") {
  auto b = fenced_pair();
  b.tracks.clear();
  for (auto& p : b.pads)
    if (p.pos.x > 20'000'000) p.pos.x = 7'000'000;
  route::RouterOptions o;
  o.diff_pairs = true;
  o.gpu_device = -1;
  o.allow_vias = false;
  o.optimize = false;
  o.rip_up = false;
  o.max_restarts = 0;
  o.work_budget = 1'000'000;
  const auto r = rules_with(200'000, false, 0, 0);
  auto run = [&](long cap) {
    o.pair_max_expansions = cap;
    return route::Router(b, r, o).run();
  };
  long low = 1, high = 4096;
  REQUIRE(run(high).pairs == 1);
  while (low < high) {
    const long middle = (low + high) / 2;
    if (run(middle).pairs == 1) high = middle;
    else low = middle + 1;
  }
  // The first cap that succeeds reaches its first legal goal on the final expansion.
  REQUIRE(low > 1);
  CHECK(run(low - 1).pairs == 0);
  const auto first = run(low), second = run(low);
  REQUIRE(first.pairs == 1);
  CHECK(first.expansions == second.expansions);
  CHECK(first.pair_work == second.pair_work);
  REQUIRE(first.tracks.size() == second.tracks.size());
  for (std::size_t i = 0; i < first.tracks.size(); ++i) {
    CHECK(first.tracks[i].a == second.tracks[i].a);
    CHECK(first.tracks[i].b == second.tracks[i].b);
    CHECK(first.tracks[i].width == second.tracks[i].width);
    CHECK(first.tracks[i].layer == second.tracks[i].layer);
    CHECK(first.tracks[i].net == second.tracks[i].net);
  }
  b.tracks.insert(b.tracks.end(), first.tracks.begin(), first.tracks.end());
  CHECK(drc::run_drc(b, r).unconnected.empty());
}

TEST_CASE("short pair tapers couple 0.5 to 1.5 mm links without relaxing clearance", "[diffpair][route]") {
  for (const Coord length : {500'000, 1'000'000, 1'500'000})
    for (const Coord spacing : {300'000, 500'000, 700'000})
      for (bool mirrored : {false, true}) {
        auto b = fenced_pair();
        b.tracks.clear();
        for (auto& p : b.pads) {
          p.size_x = p.size_y = 100'000;
          p.pos.x = p.pos.x > 20'000'000 ? 5'000'000 + length : 5'000'000;
          p.pos.y = 10'000'000 + (p.net == 1 ? -spacing / 2 : spacing / 2);
          if (mirrored) p.pos = {40'000'000 - p.pos.x, 20'000'000 - p.pos.y};
        }
        auto r = rules_with(100'000, true, 100'000, 100'000);
        r.minimums.track_width = 100'000;
        route::RouterOptions o;
        o.diff_pairs = true;
        o.allow_vias = false;
        o.gpu_device = -1;
        o.optimize = false;
        o.rip_up = false;
        o.work_budget = 5'000'000;  // geometry test: allow the complete taper matrix
        const auto result = route::Router(b, r, o).run();
        INFO(length);
        INFO(spacing);
        INFO(mirrored);
        REQUIRE(result.pairs == 1);
        const auto stats = route::measure_pair(result.tracks, result.vias, 1, 2, 150'000);
        CHECK(stats.coupled_share() >= 0.30);
        for (const auto& t : result.tracks) CHECK(t.width == 100'000);
        b.tracks.insert(b.tracks.end(), result.tracks.begin(), result.tracks.end());
        const auto report = drc::run_drc(b, r);
        CHECK(report.unconnected.empty());
        for (const auto& v : report.violations) {
          INFO(v.description);
          CHECK(v.type != "clearance");
          CHECK(v.type != "shorting_items");
        }
      }
}

TEST_CASE("blocked short taper never commits a half pair", "[diffpair][route]") {
  auto b = fenced_pair();
  b.tracks.clear();
  for (auto& p : b.pads)
    if (p.pos.x > 20'000'000) p.pos.x = 6'000'000;
  b.tracks.push_back({{5'500'000, 0}, {5'500'000, 20'000'000}, 200'000, 0, 3, true, sexpr::kNoNode});
  route::RouterOptions o;
  o.diff_pairs = true;
  o.allow_vias = false;
  o.gpu_device = -1;
  o.work_budget = 20'000;
  o.optimize = false;
  const auto result = route::Router(b, rules_with(200'000, false, 0, 0), o).run();
  CHECK(result.pairs == 0);
  CHECK(result.tracks.empty());
  CHECK(result.vias.empty());
}

namespace {

route::RouterOptions pair_finish_options(long work = 10'000'000) {
  route::RouterOptions o;
  o.diff_pairs = true;
  o.allow_vias = false;
  o.gpu_device = -1;
  o.work_budget = work;
  o.time_limit_s = 600;
  o.optimize = false;
  o.rip_up = false;
  o.max_passes = 1;
  o.max_restarts = 0;
  return o;
}

void same_pair_copper(const route::RouteResult& a, const route::RouteResult& b, bool same_work = true) {
  if (same_work) {
    CHECK(a.expansions == b.expansions);
    CHECK(a.pair_work == b.pair_work);
  }
  REQUIRE(a.tracks.size() == b.tracks.size());
  REQUIRE(a.vias.size() == b.vias.size());
  for (std::size_t i = 0; i < a.tracks.size(); ++i) {
    CHECK(a.tracks[i].a == b.tracks[i].a);
    CHECK(a.tracks[i].b == b.tracks[i].b);
    CHECK(a.tracks[i].width == b.tracks[i].width);
    CHECK(a.tracks[i].layer == b.tracks[i].layer);
    CHECK(a.tracks[i].net == b.tracks[i].net);
  }
}

model::Board straddled_pair() {
  return io::read_board(sexpr::Document::parse(R"((kicad_pcb (version 20240108) (generator "pcbnew")
    (general (thickness 1.6))
    (layers (0 "F.Cu" signal) (44 "Edge.Cuts" user))
    (setup (pad_to_mask_clearance 0))
    (net 0 "") (net 1 "D_P") (net 2 "D_N") (net 3 "GND") (net 4 "W")
    (footprint "T:P" (layer "F.Cu") (at 5 10) (property "Reference" "J1")
      (pad "1" smd rect (at 0 -0.95) (size 0.3 0.3) (layers "F.Cu") (net 1 "D_P"))
      (pad "2" smd rect (at 0 0.95) (size 0.3 0.3) (layers "F.Cu") (net 2 "D_N"))
      (pad "3" smd rect (at 0 0) (size 0.3 0.3) (layers "F.Cu") (net 3 "GND")))
    (footprint "T:P" (layer "F.Cu") (at 9 10) (property "Reference" "J2")
      (pad "1" smd rect (at 0 -0.3) (size 0.3 0.3) (layers "F.Cu") (net 1 "D_P"))
      (pad "2" smd rect (at 0 0.3) (size 0.3 0.3) (layers "F.Cu") (net 2 "D_N")))
    (footprint "T:P" (layer "F.Cu") (at 7 10) (property "Reference" "J3")
      (pad "1" smd rect (at 0 0) (size 0.3 0.3) (layers "F.Cu") (net 3 "GND")))
    (segment (start 4.3 8.5) (end 4.3 11.5) (width 0.2) (locked yes) (layer "F.Cu") (net 4))
    (segment (start 4.3 8.5) (end 4.6 8.5) (width 0.2) (locked yes) (layer "F.Cu") (net 4))
    (segment (start 4.3 11.5) (end 4.6 11.5) (width 0.2) (locked yes) (layer "F.Cu") (net 4))
    (gr_rect (start 0 0) (end 14 20) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
  ))"));
}

}  // namespace

TEST_CASE("blocked short taper falls through to a deterministic coupled search", "[diffpair][route]") {
  auto b = fenced_pair();
  b.tracks.clear();
  for (auto& p : b.pads)
    if (p.pos.x > 20'000'000) p.pos.x = 6'000'000;
  // The fixed taper families all cross this fence; a paired dogleg can go around it.
  b.tracks.push_back({{5'500'000, 9'400'000}, {5'500'000, 10'600'000}, 200'000, 0, 3, true, sexpr::kNoNode});
  const auto r = rules_with(200'000, false, 0, 0);
  const auto o = pair_finish_options();
  const auto result = route::Router(b, r, o).run();
  REQUIRE(result.pairs == 1);
  CHECK(result.pair_work > 100);
  CHECK(route::measure_pair(result.tracks, result.vias, 1, 2, 250'000).coupled_share() > 0.3);
  same_pair_copper(result, route::Router(b, r, o).run());
  const auto limited = route::Router(b, r, pair_finish_options(2'000)).run();
  CHECK(limited.pair_work <= 2'000 / 10 + 64);
  same_pair_copper(limited, route::Router(b, r, pair_finish_options(2'000)).run());
  b.tracks.insert(b.tracks.end(), result.tracks.begin(), result.tracks.end());
  CHECK(drc::run_drc(b, r).unconnected.empty());
}

TEST_CASE("pair search retries a geometrically legal finish that encloses a middle pin", "[diffpair][route]") {
  auto b = straddled_pair();
  b.copper.push_back(static_cast<int>(b.layers.size()));
  b.layers.push_back({31, "B.Cu", "B.Cu", "signal", "", 1});
  for (auto& p : b.pads)
    if (p.net == 1 || p.net == 2) {
      p.copper |= model::layer_bit(1);
      p.type = model::PadType::ThruHole;
      p.drill_x = p.drill_y = 100'000;
    }
  const auto r = rules_with(150'000, false, 0, 0);
  auto o = pair_finish_options(100'000'000);
  // Front-copper goals compete first, but they box the front-only ground pin; back copper leaves it open.
  o.layer_cost_pm = {1000, 1100};
  const auto result = route::Router(b, r, o).run();
  REQUIRE(result.pairs == 1);
  REQUIRE_FALSE(result.pair_attempts.empty());
  CHECK(result.pair_attempts.front().detail.find("would enclose pin") != std::string::npos);
  CHECK(result.routed == result.connections);
  CHECK(result.expansions <= o.work_budget + 64);
  same_pair_copper(result, route::Router(b, r, o).run());
  b.tracks.insert(b.tracks.end(), result.tracks.begin(), result.tracks.end());
  CHECK(drc::run_drc(b, r).unconnected.empty());
}

TEST_CASE("fully enclosed finish control restores copper before ordinary routing", "[diffpair][route]") {
  auto b = straddled_pair();
  // Extend the fence to the outer signal pads: no searched finish can leave the ground escape open.
  b.tracks[1].b.x = b.tracks[2].b.x = 5'000'000;
  struct RemovalSink final : events::Sink {
    long removed = 0;
    void publish(std::string message) override {
      if (message.find("\"type\":\"track_remove\"") != std::string::npos) ++removed;
    }
    bool wants_transient() const override { return false; }
  } sink;
  auto o = pair_finish_options();
  o.sink = &sink;
  const auto r = rules_with(150'000, false, 0, 0);
  const auto result = route::Router(b, r, o).run();
  REQUIRE(result.pairs == 0);
  CHECK(sink.removed > 0);
  o.sink = nullptr;
  o.diff_pairs = false;
  const auto ordinary = route::Router(b, r, o).run();
  // Failed pair insertion must leave no copper, near-obstacle or connection-state residue.
  same_pair_copper(result, ordinary, false);
  b.tracks.insert(b.tracks.end(), result.tracks.begin(), result.tracks.end());
  CHECK(drc::run_drc(b, r).unconnected.empty());
}

TEST_CASE("small midpoint displacement does not bypass the search for long asymmetric halves", "[diffpair][route]") {
  auto b = fenced_pair();
  b.tracks.clear();
  b.pads[0].pos = {5'000'000, 9'700'000};
  b.pads[2].pos = {7'000'000, 9'850'000};
  b.pads[1].pos = {9'000'000, 10'300'000};
  b.pads[3].pos = {7'000'000, 10'300'000};
  auto o = pair_finish_options(100'000);
  o.pair_max_expansions = 100;
  const auto r = rules_with(150'000, false, 0, 0);
  const auto result = route::Router(b, r, o).run();
  REQUIRE_FALSE(result.pair_attempts.empty());
  CHECK(result.pair_attempts.front().work > 0);
  CHECK(result.pair_attempts.front().reason != "short_taper_blocked");
  same_pair_copper(result, route::Router(b, r, o).run());
}

TEST_CASE("interleaved duplicate connector joins remain independent fanouts", "[diffpair][route]") {
  auto b = fenced_pair();
  b.tracks.clear();
  b.footprints.resize(1);
  b.footprints[0].pads = {0, 1, 2, 3};
  // USB-C-style B7/A6/A7/B6 row: each net joins its duplicates around the other.
  for (std::size_t i = 0; i < b.pads.size(); ++i) {
    auto& p = b.pads[i];
    p.footprint = 0;
    p.pos = {5'000'000, 9'000'000 + static_cast<Coord>(i) * 500'000};
    p.size_x = 1'450'000;
    p.size_y = 300'000;
  }
  const auto r = rules_with(100'000, true, 100'000, 100'000);
  const auto o = pair_finish_options();
  const auto result = route::Router(b, r, o).run();
  REQUIRE_FALSE(result.pair_attempts.empty());
  CHECK(result.pair_attempts.front().work == 0);
  CHECK_FALSE(result.pair_attempts.front().coupled);
  CHECK(result.pair_attempts.front().detail == "no coupled path for interleaved local terminals");
  CHECK(result.pairs == 0);
  CHECK(result.routed == result.connections);
  same_pair_copper(result, route::Router(b, r, o).run());
  b.tracks.insert(b.tracks.end(), result.tracks.begin(), result.tracks.end());
  CHECK(drc::run_drc(b, r).unconnected.empty());
}

TEST_CASE("explicit USB pairs remain selected with name detection enabled", "[diffpair][route]") {
  auto b = fenced_pair();
  b.tracks.clear();
  b.nets[1].name = "USB_DP";
  b.nets[2].name = "USB_DM";
  const auto r = rules_with(200'000, false, 0, 0);
  for (bool names : {false, true}) {
    route::RouterOptions o;
    o.gpu_device = -1;
    o.diff_pairs = names;
    o.pair_nets = {{1, 2}};
    o.optimize = false;
    o.work_budget = 2'000'000;
    const auto result = route::Router(b, r, o).run();
    REQUIRE(result.pairs == 1);
    REQUIRE(result.pair_attempts.size() == 1);
    CHECK(result.pair_successes == 1);
    CHECK(result.pair_attempts[0].coupled);
    CHECK(result.pair_attempts[0].reason == "coupled");
    CHECK(result.pair_attempts[0].work == result.pair_work);
  }
  route::RouterOptions unselected;
  unselected.gpu_device = -1;
  unselected.diff_pairs = true;
  unselected.optimize = false;
  const auto ordinary = route::Router(b, r, unselected).run();
  CHECK(ordinary.pair_attempts.empty());
  CHECK(ordinary.pairs == 0);
}
