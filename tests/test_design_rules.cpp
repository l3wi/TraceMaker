// SPDX-License-Identifier: GPL-3.0-or-later
// Custom-rule routing and DRC (doc 05 §16): disallow, physical hole clearance and via-only keepouts.
#include <catch2/catch_test_macros.hpp>

#include <filesystem>
#include <fstream>
#include <string>
#include <tuple>

#include "app/route_job.hpp"
#include "drc/drc.hpp"
#include "io/kicad/board_reader.hpp"
#include "io/kicad/project_reader.hpp"
#include "route/obstacles.hpp"
#include "route/router.hpp"

using namespace tmk;
namespace fs = std::filesystem;

namespace {

// Four layers, 20 x 10 mm; SIG and GND each connect 1 mm F.Cu pads at x = 4 (R1) and x = 16 (R2).
// `extra` adds keepouts or copper inside the board.
std::string board_text(const std::string& extra) {
  auto fp = [](const char* ref, double x) {
    return std::string("  (footprint \"R\" (layer \"F.Cu\") (at ") + std::to_string(x) + " 5)\n" +
           "    (property \"Reference\" \"" + ref + "\" (at 0 0) (layer \"F.SilkS\"))\n" +
           "    (pad \"1\" smd rect (at 0 -2) (size 1 1) (layers \"F.Cu\") (net 1 \"SIG\"))\n" +
           "    (pad \"2\" smd rect (at 0 2) (size 1 1) (layers \"F.Cu\") (net 2 \"GND\"))\n  )\n";
  };
  return "(kicad_pcb (version 20240108) (generator \"pcbnew\")\n"
         "  (layers (0 \"F.Cu\" signal) (1 \"In1.Cu\" signal) (2 \"In2.Cu\" signal) (31 \"B.Cu\" signal) (37 \"F.SilkS\" user)\n"
         "    (44 \"Edge.Cuts\" user))\n"
         "  (net 0 \"\") (net 1 \"SIG\") (net 2 \"GND\")\n" +
         fp("R1", 4) + fp("R2", 16) +
         "  (gr_rect (start 0 0) (end 20 10) (layer \"Edge.Cuts\") (stroke (width 0.1) (type solid)))\n" + extra + ")\n";
}

// A keepout on the outer layers across the middle (x 9..11): connections must cross it on an inner layer.
const char* kOuterWall =
    "  (zone (net 0) (net_name \"\") (layers \"F.Cu\" \"B.Cu\") (name \"wall\") (hatch edge 0.5) (connect_pads (clearance 0))\n"
    "    (min_thickness 0.25) (keepout (tracks not_allowed) (vias not_allowed) (pads allowed) (copperpour allowed) (footprints allowed))\n"
    "    (fill (thermal_gap 0.5) (thermal_bridge_width 0.5)) (polygon (pts (xy 9 0) (xy 11 0) (xy 11 10) (xy 9 10))))\n";

const char* kInnerGndOnly =
    "(version 1)\n(rule \"inner GND only\" (layer inner) (condition \"A.NetName != 'GND'\") (constraint disallow track))\n";

struct Files {
  fs::path dir, pcb;
  Files(const std::string& name, const std::string& board, const std::string& dru) {
    dir = fs::temp_directory_path() / ("tmk_design_rules_" + name);
    fs::remove_all(dir);
    fs::create_directories(dir);
    pcb = dir / "b.kicad_pcb";
    std::ofstream(pcb) << board;
    if (!dru.empty()) std::ofstream(dir / "b.kicad_dru") << dru;
  }
  ~Files() { fs::remove_all(dir); }
};

route::RouteResult route_board(const Files& f) {
  const auto lb = io::read_board_file(f.pcb.string());
  const auto rules = io::read_design_rules(f.pcb.string());
  route::RouterOptions o;
  o.work_budget = 2'000'000;
  o.gpu_device = -1;
  o.time_limit_s = 600;
  return route::Router(lb.board, rules, o).run();
}

bool has_track(const route::RouteResult& r, model::NetId net, bool inner) {
  for (const auto& t : r.tracks)
    if (t.net == net && (t.layer == 1 || t.layer == 2) == inner) return true;
  return false;
}

}  // namespace

TEST_CASE("disallow track on inner layers: other nets avoid them, GND may still use them", "[rules][route]") {
  const Files free_board("free", board_text(kOuterWall), "");
  const auto before = route_board(free_board);
  CHECK(before.routed == 2);  // both nets cross the wall on an inner layer
  CHECK(has_track(before, 1, true));

  const Files ruled("ruled", board_text(kOuterWall), kInnerGndOnly);
  const auto after = route_board(ruled);
  CHECK(after.routed == 1);             // SIG cannot cross the wall
  CHECK_FALSE(has_track(after, 1, true));
  CHECK(has_track(after, 2, true));      // GND still uses an inner layer
}

TEST_CASE("physical_hole_clearance keeps vias out of same-net SMD pads, except where the condition excludes them",
          "[rules][route]") {
  const std::string rule =
      "(rule \"no via in pad\" (constraint physical_hole_clearance (min 0.05mm))\n"
      "  (condition \"A.Type == 'Via' && B.Type == 'Pad' && B.Pad_Type == 'SMD' && B.Reference != 'R2'\"))\n";
  const Files f("vip", board_text(""), rule);
  auto lb = io::read_board_file(f.pcb.string());
  const auto with_rule = io::read_design_rules(f.pcb.string());
  const model::DesignRules no_rule = [&] {
    auto r = with_rule;
    r.custom.clear();
    return r;
  }();
  const geom::Point r1_sig{4'000'000, 3'000'000}, r2_sig{16'000'000, 3'000'000};
  const Coord d = 600'000, drill = 300'000;
  {
    route::Obstacles obs(lb.board, no_rule);
    CHECK(obs.via_state(r1_sig, d, drill, 1, 0, false) == 0);  // KiCad's default rules allow a via in its own pad
  }
  route::Obstacles obs(lb.board, with_rule);
  CHECK(obs.via_state(r1_sig, d, drill, 1, 0, false) == 2);
  CHECK(obs.fixed_via_code(r1_sig, d, drill, 0, 1) == route::Obstacles::kBlocked);  // cached path agrees
  CHECK(obs.via_state(r2_sig, d, drill, 1, 0, false) == 0);                      // R2 is excluded
  CHECK(obs.via_state({4'000'000, 4'300'000}, d, drill, 1, 0, false) == 0);        // beside the pad
  CHECK_FALSE(obs.needs_exact_routing());  // net-independent rule: cache remains valid
}

TEST_CASE("a keepout that forbids only vias blocks vias and lets tracks through", "[rules][route]") {
  const std::string ko =
      "  (zone (net 0) (net_name \"\") (layers \"B.Cu\") (name \"tc\") (hatch edge 0.5) (connect_pads (clearance 0))\n"
      "    (min_thickness 0.25) (keepout (tracks allowed) (vias not_allowed) (pads allowed) (copperpour not_allowed) (footprints allowed))\n"
      "    (fill (thermal_gap 0.5) (thermal_bridge_width 0.5)) (polygon (pts (xy 8 4) (xy 12 4) (xy 12 6) (xy 8 6))))\n";
  const Files f("viako", board_text(ko), "");
  auto lb = io::read_board_file(f.pcb.string());
  const auto rules = io::read_design_rules(f.pcb.string());
  route::Obstacles obs(lb.board, rules);
  const geom::Point inside{10'000'000, 5'000'000};
  CHECK(obs.via_state(inside, 600'000, 300'000, 1, 0, false) == 2);
  CHECK(obs.fixed_via_code(inside, 600'000, 300'000, 0, 1) == route::Obstacles::kBlocked);
  CHECK(obs.segment_state({8'500'000, 5'000'000}, {11'500'000, 5'000'000}, 3, 250'000, 1, false) == 0);
}

TEST_CASE("disallow via also forbids blind and buried vias", "[rules][route]") {
  const std::string rule = "(version 1)\n(rule \"no SIG vias\" (condition \"A.NetName == 'SIG'\") (constraint disallow via))\n";
  const Files f("noblind", board_text(kOuterWall), rule);
  const auto lb = io::read_board_file(f.pcb.string());
  auto rules = io::read_design_rules(f.pcb.string());
  rules.minimums.allow_blind_buried_vias = true;
  route::RouterOptions o;
  o.work_budget = 2'000'000;
  o.gpu_device = -1;
  o.time_limit_s = 600;
  o.blind_vias = true;
  const auto r = route::Router(lb.board, rules, o).run();
  for (const auto& v : r.vias) CHECK(v.net != 1);
  CHECK(r.routed == 1);  // SIG needs a via to cross the wall; GND does not care
}

TEST_CASE("fixed via codes: the one-pass check equals the per-layer reference", "[rules][route]") {
  // Keepouts (outer wall, via-only), fixed tracks of both nets on several layers, and a physical hole rule.
  const std::string extra = std::string(kOuterWall) +
                            "  (zone (net 0) (net_name \"\") (layers \"B.Cu\") (name \"tc\") (hatch edge 0.5) (connect_pads (clearance 0))\n"
                            "    (min_thickness 0.25) (keepout (tracks allowed) (vias not_allowed) (pads allowed) (copperpour not_allowed) (footprints allowed))\n"
                            "    (fill (thermal_gap 0.5) (thermal_bridge_width 0.5)) (polygon (pts (xy 13 6) (xy 15 6) (xy 15 8) (xy 13 8))))\n"
                            "  (segment (start 2 8) (end 18 8) (width 0.25) (layer \"In1.Cu\") (net 1))\n"
                            "  (segment (start 5 1) (end 5 9) (width 0.3) (layer \"B.Cu\") (net 2))\n"
                            "  (segment (start 1 1.5) (end 19 1.5) (width 0.2) (layer \"F.Cu\") (net 2))\n";
  const std::string rule =
      "(version 1)\n(rule \"no via in pad\" (constraint physical_hole_clearance (min 0.05mm))\n"
      "  (condition \"A.Type == 'Via' && B.Type == 'Pad' && B.Pad_Type == 'SMD' && B.Reference != 'R2'\"))\n";
  const Files f("vref", board_text(extra), rule);
  auto lb = io::read_board_file(f.pcb.string());
  const auto rules = io::read_design_rules(f.pcb.string());
  route::Obstacles obs(lb.board, rules);
  int compared = 0, blocked = 0, coded = 0;
  for (const auto& [d, drill, margin] : {std::tuple<Coord, Coord, Coord>{600'000, 300'000, 0}, {450'000, 200'000, 36'000}}) {
    for (Coord y = -500'000; y <= 10'500'000; y += 130'000)
      for (Coord x = -500'000; x <= 20'500'000; x += 130'000)
        for (model::NetId net : {1, 2}) {
          const geom::Point p{x, y};
          const auto fast = obs.fixed_via_code(p, d, drill, margin, net);
          REQUIRE(fast == obs.fixed_via_code_reference(p, d, drill, margin, net));
          ++compared;
          blocked += fast == route::Obstacles::kBlocked;
          coded += fast > 0;
        }
  }
  // The sample covers every outcome: free, blocked, and legal only for one net.
  CHECK(blocked > 0);
  CHECK(coded > 0);
  CHECK(blocked + coded < compared);
}

TEST_CASE("DRC reports disallowed tracks and vias in pads as KiCad does", "[rules][drc]") {
  const std::string copper =
      "  (segment (start 6 3) (end 14 3) (width 0.25) (layer \"In1.Cu\") (net 1))\n"
      "  (segment (start 6 7) (end 14 7) (width 0.25) (layer \"In2.Cu\") (net 2))\n"
      "  (via (at 4 3) (size 0.6) (drill 0.3) (layers \"F.Cu\" \"B.Cu\") (net 1))\n";
  const std::string rules = std::string(kInnerGndOnly) +
                            "(rule \"no via in pad\" (constraint physical_hole_clearance (min 0.05mm))\n"
                            "  (condition \"A.Type == 'Via' && B.Type == 'Pad' && B.Pad_Type == 'SMD'\"))\n";
  auto count = [](const drc::DrcReport& r, const std::string& type) {
    int n = 0;
    for (const auto& v : r.violations) n += v.type == type;
    return n;
  };
  const Files plain("drc_plain", board_text(copper), "");
  const auto lb0 = io::read_board_file(plain.pcb.string());
  const auto r0 = drc::run_drc(lb0.board, io::read_design_rules(plain.pcb.string()));
  CHECK(count(r0, "items_not_allowed") == 0);
  CHECK(count(r0, "hole_clearance") == 0);

  const Files ruled("drc_ruled", board_text(copper), rules);
  const auto lb = io::read_board_file(ruled.pcb.string());
  const auto r = drc::run_drc(lb.board, io::read_design_rules(ruled.pcb.string()));
  CHECK(count(r, "items_not_allowed") == 1);  // the SIG track on In1; the GND track on In2 is allowed
  CHECK(count(r, "hole_clearance") == 1);     // the via in R1's SIG pad (same net)
}

TEST_CASE("KiCad size conditions use local pad dimensions, relational operators and units", "[rules]") {
  const Files f("size_expr", board_text(""), "");
  auto lb = io::read_board_file(f.pcb.string());
  lb.board.pads[0].size_x = 500'000;
  lb.board.pads[0].size_y = 1'000'000;
  lb.board.pads[0].angle = 90;
  lb.board.footprints[0].angle = 45;
  const auto copper = drc::build_copper(lb.board);
  const drc::CopperItem* pad = nullptr;
  for (const auto& it : copper.items)
    if (it.kind == drc::ItemKind::Pad && it.index == 0) pad = &it;
  REQUIRE(pad != nullptr);
  drc::CopperItem via;
  via.kind = drc::ItemKind::Via;
  via.net = 1;
  auto matches = [&](const std::string& expr) {
    auto rules = io::read_design_rules(f.pcb.string());
    model::CustomRule rule;
    rule.name = "size";
    rule.condition = expr;
    rule.constraints.push_back({"physical_hole_clearance", 350'000, {}, {}, {}});
    rules.custom.push_back(rule);
    const drc::RuleEngine re(lb.board, rules);
    REQUIRE(re.warnings().empty());
    CHECK_FALSE(re.needs_exact_routing());
    return re.physical_hole_clearance(&via, *pad, 0) == 350'000;
  };
  CHECK(matches("B.Size_X < 2mm && B.Size_Y <= 1mm"));
  CHECK(matches("B.Size_X >= 0.5mm && B.Size_Y > 0.5mm"));
  CHECK(matches("B.Size_X == 500000 && B.Size_Y != 500000"));
  CHECK(matches("B.Size_X == 0.01968503937007874in && B.Size_Y == 1000000"));
  CHECK(matches("0.5mil < B.Size_X && B.Size_Y >= 39mil"));
  CHECK(matches("(A.Type == 'Via') && (B.Pad_Type == 'SMD') && B.Size_X < 2 mm"));
  CHECK_FALSE(matches("B.Size_X > 1mm || B.Size_Y < 0.5mm"));
  CHECK_FALSE(matches("B.Unknown < 2mm"));
  CHECK_FALSE(matches("B.Unknown == 'anything'"));
  CHECK_FALSE(matches("B.Unknown != 'anything'"));
  CHECK_FALSE(matches("A.Type == 'Via' && A.Size_X < 2mm"));
}

TEST_CASE("Width comparisons with explicit KiCad units retain their previous results", "[rules]") {
  const Files f("width_expr", board_text(""), "");
  const auto lb = io::read_board_file(f.pcb.string());
  drc::CopperItem track;
  track.kind = drc::ItemKind::Track;
  track.net = 1;
  track.width = 200'000;
  auto matches = [&](const std::string& expr) {
    auto rules = io::read_design_rules(f.pcb.string());
    model::CustomRule rule;
    rule.condition = expr;
    rule.constraints.push_back({"physical_hole_clearance", 50'000, {}, {}, {}});
    rules.custom.push_back(rule);
    const drc::RuleEngine re(lb.board, rules);
    REQUIRE(re.warnings().empty());
    return re.physical_hole_clearance(&track, track, 0) == 50'000;
  };
  // Before D62, atof("0.2mm") == 0.2 and Width was 0.2; explicit-unit equality stays true.
  CHECK(matches("A.Width == 0.2mm"));
  CHECK(matches("A.Width != 0.25mm"));
  CHECK_FALSE(matches("A.Width != 0.2mm"));
  CHECK_FALSE(matches("A.Width == 0.25mm"));
  CHECK(matches("A.Width == 200000"));
  CHECK_FALSE(matches("A.Width == 0.2"));
}

TEST_CASE("length literals round to integer nm at pad and Width comparison boundaries", "[rules]") {
  const Files f("literal_rounding", board_text(""), "");
  auto lb = io::read_board_file(f.pcb.string());
  const auto base_rules = io::read_design_rules(f.pcb.string());
  for (const auto& [size, literal] : {std::pair<Coord, const char*>{1'001'000, "1.001mm"},
                                    {290'000, "0.29mm"}, {2'000'000, "2mm"}, {999'999'000, "999.999mm"},
                                    {1'001'000, "39.40944881889764mil"}, {1'001'000, "0.03940944881889764in"},
                                    {1'001'000, "1.0010004mm"}}) {
    CAPTURE(size, literal);
    lb.board.pads[0].size_x = lb.board.pads[0].size_y = size;
    const auto copper = drc::build_copper(lb.board);
    const drc::CopperItem* pad = nullptr;
    for (const auto& it : copper.items)
      if (it.kind == drc::ItemKind::Pad && it.index == 0) pad = &it;
    REQUIRE(pad != nullptr);
    drc::CopperItem track;
    track.kind = drc::ItemKind::Track;
    track.net = 1;
    track.width = size;
    auto matches = [&](const std::string& expr) {
      auto rules = base_rules;
      model::CustomRule rule;
      rule.condition = "A.Type == 'Track' && B.Type == 'Pad' && (" + expr + ")";
      rule.constraints.push_back({"physical_hole_clearance", 50'000, {}, {}, {}});
      rules.custom.push_back(rule);
      const drc::RuleEngine re(lb.board, rules);
      REQUIRE(re.warnings().empty());
      return re.physical_hole_clearance(&track, *pad, 0) == 50'000;
    };
    for (const std::string prop : {"B.Size_X", "B.Size_Y", "A.Width"}) {
      CAPTURE(prop);
      CHECK(matches(prop + " <= " + literal));
      CHECK(matches(prop + " >= " + literal));
      CHECK(matches(prop + " == " + literal));
      CHECK_FALSE(matches(prop + " < " + literal));
      CHECK_FALSE(matches(prop + " > " + literal));
    }
    CHECK(matches("0.0015ps == 1.5 && 1.5fs == 1.5 && 1.5deg == 1.5"));
    if (size == 1'001'000) {
      CHECK(matches("A.Width < 1001000.4"));
      CHECK_FALSE(matches("A.Width == 1001000.4"));
    }
  }
}

TEST_CASE("synthetic pad rules fail closed while project parse failures remain warnings", "[rules]") {
  const Files f("origin", board_text(""), "");
  const auto lb = io::read_board_file(f.pcb.string());
  auto rules = io::read_design_rules(f.pcb.string());
  auto rule = app::keep_vias_off_pads_rule(rules, 2'000'000);
  CHECK(rule.origin == model::RuleOrigin::Synthetic);
  CHECK(rule.constraints.front().min == 350'000);
  auto bigger = rules.default_class();
  bigger.via_diameter = 900'001;
  bigger.via_drill = 400'000;
  bigger.clearance = 250'000;
  rules.classes.push_back(bigger);
  CHECK(app::keep_vias_off_pads_rule(rules, 2'000'000).constraints.front().min == 500'001);
  rule.condition = "B.Size_X <";
  rules.custom.push_back(rule);
  CHECK_THROWS_AS(drc::RuleEngine(lb.board, rules), std::runtime_error);
  rules.custom.back().origin = model::RuleOrigin::Project;
  const drc::RuleEngine re(lb.board, rules);
  CHECK(re.warnings().size() == 1);
  CHECK_THROWS_AS(app::keep_vias_off_pads_rule(rules, 0), std::invalid_argument);
}

TEST_CASE("synthetic pad rule clears whole via copper on cached and reference paths", "[rules][route]") {
  const Files f("synthetic", board_text(""), "");
  auto lb = io::read_board_file(f.pcb.string());
  // Leave R1 small; R2 is an exposed pad, and an elongated pad is exempt if either axis reaches X.
  lb.board.pads[2].size_x = lb.board.pads[2].size_y = 4'000'000;
  lb.board.pads[3].size_x = 2'000'000;
  auto rules = io::read_design_rules(f.pcb.string());
  rules.custom.push_back(app::keep_vias_off_pads_rule(rules, 2'000'000));
  route::Obstacles obs(lb.board, rules);
  CHECK_FALSE(obs.needs_exact_routing());
  for (model::NetId net : {1, 2}) {
    CHECK(obs.via_state({4'000'000, 3'000'000}, 600'000, 300'000, net, 0, false) == 2);
    CHECK(obs.via_state({4'800'000, 3'000'000}, 600'000, 300'000, net, 0, false) == 2);  // copper touches pad
    CHECK(obs.via_state({4'999'999, 3'000'000}, 600'000, 300'000, net, 0, false) == 2);
    CHECK(obs.via_state({5'000'000, 3'000'000}, 600'000, 300'000, net, 0, false) == 0);
    CHECK(obs.via_state({5'000'001, 3'000'000}, 600'000, 300'000, net, 0, false) == 0);
  }
  CHECK(obs.via_state({16'000'000, 3'000'000}, 600'000, 300'000, 1, 0, false) == 0);
  CHECK(obs.via_state({16'000'000, 7'000'000}, 600'000, 300'000, 2, 0, false) == 0);
  for (Coord y = 2'000'000; y <= 8'000'000; y += 130'000)
    for (Coord x = 3'000'000; x <= 18'000'000; x += 130'000)
      for (model::NetId net : {1, 2})
        for (const auto& [size, drill, margin] : {std::tuple<Coord, Coord, Coord>{600'000, 300'000, 0},
                                                {450'000, 200'000, 36'000}})
          REQUIRE(obs.fixed_via_code({x, y}, size, drill, margin, net) ==
                  obs.fixed_via_code_reference({x, y}, size, drill, margin, net));
  // The last synthetic minimum must not weaken a stricter project rule.
  model::CustomRule strict;
  strict.constraints.push_back({"physical_hole_clearance", 700'000, {}, {}, {}});
  rules.custom.insert(rules.custom.begin(), strict);
  route::Obstacles stronger(lb.board, rules);
  CHECK(stronger.via_state({5'000'000, 3'000'000}, 600'000, 300'000, 1, 0, false) == 2);
}

TEST_CASE("a 0402 plane connection uses a dog-bone rather than via-in-pad", "[rules][route]") {
  const std::string text =
      "(kicad_pcb (version 20240108) (generator \"pcbnew\")"
      " (layers (0 \"F.Cu\" signal) (31 \"B.Cu\" signal) (37 \"F.SilkS\" user) (44 \"Edge.Cuts\" user))"
      " (net 0 \"\") (net 1 \"GND\")"
      " (footprint \"0402\" (layer \"F.Cu\") (at 4 3)"
      " (property \"Reference\" \"R1\" (at 0 0) (layer \"F.SilkS\"))"
      " (pad \"1\" smd rect (at 0 0) (size 0.5 0.6) (layers \"F.Cu\") (net 1 \"GND\")))"
      " (footprint \"plane-anchor\" (layer \"B.Cu\") (at 16 3)"
      " (property \"Reference\" \"J1\" (at 0 0) (layer \"F.SilkS\"))"
      " (pad \"1\" smd rect (at 0 0) (size 3 3) (layers \"B.Cu\") (net 1 \"GND\")))"
      " (zone (net 1) (net_name \"GND\") (layer \"B.Cu\") (hatch edge 0.5) (connect_pads (clearance 0.2))"
      " (min_thickness 0.25) (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))"
      " (polygon (pts (xy 1 1) (xy 19 1) (xy 19 9) (xy 1 9)))"
      " (filled_polygon (layer \"B.Cu\") (pts (xy 1 1) (xy 19 1) (xy 19 9) (xy 1 9))))"
      " (gr_rect (start 0 0) (end 20 10) (layer \"Edge.Cuts\") (stroke (width 0.1) (type solid))))";
  const Files f("dogbone", text, "");
  bool raised_via = false;
  SECTION("net-class via dimensions") {}
  SECTION("board minimums raise the via diameter and annular ring") {
    raised_via = true;
    std::ofstream(f.dir / "b.kicad_pro") <<
        R"({"board":{"design_settings":{"rules":{"min_via_diameter":1.5,"min_via_annular_width":0.3}}}})";
    const auto rules = io::read_design_rules(f.pcb.string());
    REQUIRE(rules.default_class().via_diameter == 600'000);
    REQUIRE(rules.default_class().via_drill == 300'000);
    CHECK(app::keep_vias_off_pads_rule(rules, 2'000'000).constraints.front().min == 800'000);
  }
  app::RouteJob job;
  job.in = f.pcb.string();
  job.threads = job.variants = 1;
  job.opt.gpu_device = -1;
  job.opt.work_budget = 1'000'000;
  job.opt.time_limit_s = 600;
  job.opt.pitch = 50'000;
  const auto baseline = app::run_route_job(job);
  REQUIRE(baseline.result.routed == 1);
  REQUIRE(baseline.result.vias.size() == 1);
  CHECK(baseline.result.vias.front().size == (raised_via ? 1'500'000 : 600'000));
  CHECK(baseline.result.vias.front().drill == 300'000);
  const auto lb = io::read_board_file(job.in);
  const auto cm = drc::build_copper(lb.board);
  const geom::Shape& pad = cm.items.front().shapes.front();
  CHECK(geom::closer_than_disk(pad, baseline.result.vias.front().pos, baseline.result.vias.front().size / 2, 0));
  job.opt.keep_vias_off_pads = 2'000'000;
  job.out = (f.dir / "routed.kicad_pcb").string();
  const auto dogbone = app::run_route_job(job);
  REQUIRE(dogbone.result.routed == 1);
  REQUIRE(dogbone.result.vias.size() == 1);
  CHECK(dogbone.result.vias.front().size == (raised_via ? 1'500'000 : 600'000));
  CHECK(dogbone.result.vias.front().drill == 300'000);
  CHECK_FALSE(dogbone.result.tracks.empty());
  for (const auto& v : dogbone.result.vias)
    CHECK_FALSE(geom::closer_than_disk(pad, v.pos, v.size / 2, 200'000));
  const auto routed = io::read_board_file(job.out);
  const auto project_rules = io::read_design_rules(job.out);
  CHECK(project_rules.custom.empty());  // route-only: never writes .kicad_dru or changes `tracemaker drc`
  const auto report = drc::run_drc(routed.board, project_rules);
  CHECK(report.violations.empty());
  CHECK(report.unconnected.empty());
}

TEST_CASE("class via dimensions include board drill, diameter and annular minimums", "[rules][route]") {
  const Files f("effective_via", board_text(""), "");
  auto rules = io::read_design_rules(f.pcb.string());
  auto check = [&](Coord diameter, Coord drill, Coord margin) {
    const auto via = route::class_via(rules, rules.default_class());
    CHECK(via.diameter == diameter);
    CHECK(via.drill == drill);
    CHECK(app::keep_vias_off_pads_rule(rules, 2'000'000).constraints.front().min == margin);
  };
  check(600'000, 300'000, 350'000);
  rules.minimums.through_hole_diameter = 500'000;
  check(700'000, 500'000, 300'000);  // the default board annular minimum is 0.1 mm
  rules.minimums.via_annular_width = 300'000;
  check(1'100'000, 500'000, 500'000);
  rules.minimums.via_diameter = 1'500'000;
  check(1'500'000, 500'000, 700'000);
  rules.minimums.through_hole_diameter = 0;
  check(1'500'000, 300'000, 800'000);
}
