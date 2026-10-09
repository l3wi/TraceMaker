// SPDX-License-Identifier: GPL-3.0-or-later
// tracemaker: command-line front end. Subcommands grow with the roadmap (route, place, bench, serve, replay).
#include <CLI/CLI.hpp>

#include <cstdio>
#include <filesystem>
#include <fstream>
#include <map>
#include <nlohmann/json.hpp>
#include <string>

#include "app/defects.hpp"
#include "app/inspect.hpp"
#include "app/route_job.hpp"
#include "core/rng.hpp"
#include "crules/engine.hpp"
#include "drc/drc.hpp"
#include "io/kicad/project_reader.hpp"
#include "route/router.hpp"
#include "route/diff_pair.hpp"
#include "route/escape.hpp"
#include "route/escape_flow.hpp"
#include "route/obstacles.hpp"
#include "core/version.hpp"
#include "gpu/device.hpp"
#include "io/kicad/board_editor.hpp"
#include "io/kicad/board_reader.hpp"

namespace {

int cmd_gpu_info() {
  const auto backend = tmk::gpu::compiled_backend();
  if (backend == tmk::gpu::Backend::Cpu) {
    std::puts("This build has no GPU support (cpu-only). CPU reference paths are used.");
    return 0;
  }
  const auto devices = tmk::gpu::list_devices();
  if (devices.empty()) {
    std::puts("No GPU devices visible. CPU reference paths will be used.");
    return 0;
  }
  constexpr double kMiB = 1024.0 * 1024.0;
  for (const auto& d : devices) {
    if (backend == tmk::gpu::Backend::Cuda) {
      std::printf("cuda:%d  %-24s sm_%d%d  free %7.0f / %7.0f MiB  %s\n", d.index, d.name.c_str(), d.cc_major,
                  d.cc_minor, static_cast<double>(d.free_bytes) / kMiB, static_cast<double>(d.total_bytes) / kMiB,
                  d.uuid.c_str());
    } else {
      std::printf("metal:%d  %-24s working-set headroom %7.0f / %7.0f MiB  %s\n", d.index, d.name.c_str(),
                  static_cast<double>(d.free_bytes) / kMiB, static_cast<double>(d.total_bytes) / kMiB, d.uuid.c_str());
    }
  }
  return 0;
}

// Applies a fixed set of edits (used by tests/integration to check edited files against KiCad):
// moves and rotates the first two unlocked footprints, removes the first track and via, adds a track and a via.
int cmd_selftest_edit(const std::string& in, const std::string& out) {
  auto lb = tmk::io::read_board_file(in);
  tmk::io::BoardEditor ed(lb, 7);
  auto& b = lb.board;
  int moved = 0;
  for (std::size_t i = 0; i < b.footprints.size() && moved < 2; ++i) {
    const auto& f = b.footprints[i];
    if (f.locked) continue;
    ed.move_footprint(i, {f.pos.x + 1'270'000, f.pos.y - 635'000}, f.angle + (moved == 0 ? 90.0 : 45.0));
    ++moved;
  }
  if (!b.tracks.empty()) ed.remove_track(0);
  if (!b.vias.empty()) ed.remove_via(0);
  const tmk::model::NetId net = b.nets.size() > 1 ? 1 : 0;
  ed.add_track({{10'000'000, 10'000'000}, {12'500'000, 12'500'000}, 250'000, 0, net, false, tmk::sexpr::kNoNode});
  ed.add_via({{12'500'000, 12'500'000}, 600'000, 300'000, 0, b.copper_count() - 1, tmk::model::ViaType::Through, net,
              false, tmk::sexpr::kNoNode});
  ed.save(out);
  std::printf("edited %s -> %s (%d footprints moved)\n", in.c_str(), out.c_str(), moved);
  return 0;
}

// Random perturbations that create DRC violations, for parity testing against KiCad's DRC:
// tracks between random pads, vias at random places, and small footprint moves.
int cmd_perturb(const std::string& in, const std::string& out, std::uint64_t seed, int tracks, int vias, int moves) {
  auto lb = tmk::io::read_board_file(in);
  tmk::io::BoardEditor ed(lb, seed);
  const auto& b = lb.board;
  const tmk::RngStream rng(seed, 0x9e27u, 0);
  std::uint64_t k = 0;
  auto u = [&](std::uint64_t n) { return n ? rng.u64(k++) % n : 0; };
  const auto bb = b.edge_bbox().empty() ? tmk::geom::Box{0, 0, 100'000'000, 100'000'000} : b.edge_bbox();
  auto rnd_pt = [&]() {
    return tmk::model::Point{bb.x0 + static_cast<tmk::Coord>(u(static_cast<std::uint64_t>(bb.x1 - bb.x0))),
                             bb.y0 + static_cast<tmk::Coord>(u(static_cast<std::uint64_t>(bb.y1 - bb.y0)))};
  };
  static const tmk::Coord widths[] = {80'000, 100'000, 150'000, 200'000, 250'000, 400'000};
  for (int i = 0; i < tracks && !b.pads.empty(); ++i) {
    const auto& p = b.pads[u(b.pads.size())];
    const tmk::model::Point a = p.pos;
    const tmk::model::Point c{a.x + static_cast<tmk::Coord>(u(6'000'000)) - 3'000'000, a.y + static_cast<tmk::Coord>(u(6'000'000)) - 3'000'000};
    int layer = 0;
    for (int l = 0; l < b.copper_count(); ++l)
      if (p.copper & tmk::model::layer_bit(l)) { layer = l; break; }
    ed.add_track({a, c, widths[u(6)], layer, p.net, false, tmk::sexpr::kNoNode});
  }
  static const tmk::Coord vsizes[][2] = {{600'000, 300'000}, {800'000, 400'000}, {450'000, 300'000}, {300'000, 200'000}};
  for (int i = 0; i < vias; ++i) {
    const auto& vs = vsizes[u(4)];
    const tmk::model::NetId net = static_cast<tmk::model::NetId>(u(b.nets.size()));
    ed.add_via({rnd_pt(), vs[0], vs[1], 0, b.copper_count() - 1, tmk::model::ViaType::Through, net, false, tmk::sexpr::kNoNode});
  }
  for (int i = 0; i < moves && !b.footprints.empty(); ++i) {
    const std::size_t fi = u(b.footprints.size());
    const auto& f = b.footprints[fi];
    if (f.locked) continue;
    ed.move_footprint(fi, {f.pos.x + static_cast<tmk::Coord>(u(3'000'000)) - 1'500'000, f.pos.y + static_cast<tmk::Coord>(u(3'000'000)) - 1'500'000}, f.angle);
  }
  ed.save(out);
  return 0;
}

int cmd_drc(const std::string& path, const std::string& json_out, tmk::Coord epsilon, bool linear_zones, const std::string& project) {
  const auto lb = tmk::io::read_board_file(path);
  const auto rules = tmk::io::read_design_rules(path, project);
  tmk::drc::DrcOptions opt;
  opt.epsilon = epsilon;
  opt.linear_zone_tests = linear_zones;
  const auto rep = tmk::drc::run_drc(lb.board, rules, opt);
  for (const auto& [type, n] : rep.counts()) std::printf("%-24s %d\n", type.c_str(), n);
  for (const auto& w : rep.warnings) std::printf("warning: %s\n", w.c_str());
  if (!json_out.empty()) tmk::drc::write_drc_json(rep, json_out);
  return rep.violations.empty() && rep.unconnected.empty() ? 0 : 5;
}

// Component-aware layout rules (design doc 15 §7): detections, bound roles and every rule's status. Read-only.
int cmd_rules(const std::string& path, const std::string& mode_s, const std::string& json_out, const std::string& dru_out, const std::string& roles_from,
              const std::string& catalogue_path, const std::string& override_path, const std::string& project) {
  namespace cr = tmk::crules;
  const auto lb = tmk::io::read_board_file(path);
  const auto rules = tmk::io::read_design_rules(path, project);
  const cr::Mode mode = cr::parse_mode(mode_s);
  cr::Catalogue own;
  if (!catalogue_path.empty()) own = cr::load_catalogue_file(catalogue_path);
  const cr::Catalogue& cat = catalogue_path.empty() ? cr::builtin_catalogue() : own;
  cr::Overrides ov;
  if (!override_path.empty()) ov = cr::load_overrides_file(override_path, cat);
  const cr::Overrides* ovp = override_path.empty() ? nullptr : &ov;
  cr::Detection det;
  if (!roles_from.empty()) {
    // Detect and bind on another version of the same board (e.g. the input of a placement), measure on this one:
    // binding that depends on positions (regulator caps) then names the same parts in both.
    const auto ref = tmk::io::read_board_file(roles_from);
    const auto& a = ref.board;
    bool same = a.footprints.size() == lb.board.footprints.size() && a.pads.size() == lb.board.pads.size() && a.nets.size() == lb.board.nets.size();
    for (std::size_t i = 0; same && i < a.footprints.size(); ++i) same = a.footprints[i].reference == lb.board.footprints[i].reference;
    for (std::size_t i = 0; same && i < a.nets.size(); ++i) same = a.nets[i].name == lb.board.nets[i].name;
    if (!same) throw std::runtime_error("--roles-from: " + roles_from + " is not the same board (footprints, pads or nets differ)");
    det = cr::detect(a, cat, ovp);
  } else {
    det = cr::detect(lb.board, cat, ovp);
  }
  const cr::Evaluation ev = cr::evaluate(lb.board, &rules, cat, det, mode);
  std::fputs(cr::report_text(lb.board, cat, det, ev).c_str(), stdout);
  if (!json_out.empty()) std::ofstream(json_out) << cr::report_json(lb.board, cat, det, ev).dump(1) << "\n";
  if (!dru_out.empty()) {
    std::ofstream(dru_out) << cr::dru_sidecar(lb.board, cat, det);
    std::printf("generated custom rules written to %s\n", dru_out.c_str());
  }
  return 0;
}

int cmd_route(tmk::app::RouteJob job) {
  job.log = [](const std::string& line) {
    std::printf("%s\n", line.c_str());
    std::fflush(stdout);
  };
  return tmk::app::run_route_job(std::move(job)).exit_code();
}

// Prints the fixed-obstacle legality map around a pad (router debugging): '.' free for its net, '#' blocked,
// '+' the pad centre.
int cmd_debug_pad(const std::string& path, const std::string& ref, const std::string& num, double pitch_mm, double radius_mm, double width_mm,
                  bool via_map) {
  auto lb = tmk::io::read_board_file(path);
  const auto rules = tmk::io::read_design_rules(path);
  tmk::model::Board b = lb.board;
  tmk::route::Obstacles obs(b, rules);
  for (const auto& p : b.pads) {
    const auto& fp = b.footprints[static_cast<std::size_t>(p.footprint)];
    if (fp.reference != ref || p.number != num) continue;
    const auto& nc = rules.class_for(b.nets[static_cast<std::size_t>(p.net)].name);
    const tmk::Coord hw = width_mm > 0 ? static_cast<tmk::Coord>(width_mm * 5e5) : std::max(nc.track_width, rules.minimums.track_width) / 2;
    const tmk::Coord pitch = static_cast<tmk::Coord>(pitch_mm * 1e6);
    const int n = static_cast<int>(radius_mm / pitch_mm);
    for (int l = 0; l < b.copper_count(); ++l) {
      if (!(p.copper & tmk::model::layer_bit(l))) continue;
      std::printf("%s.%s net %s layer %s hw %.3f mm; centre inside board: %d; outline points %zu\n", ref.c_str(), num.c_str(),
                  b.nets[static_cast<std::size_t>(p.net)].name.c_str(), b.copper_name(l).c_str(), tmk::nm_to_mm(hw), obs.inside_board(p.pos, 0),
                  obs.outline().size());
      for (int y = -n; y <= n; ++y) {
        std::string row;
        for (int x = -n; x <= n; ++x) {
          const tmk::geom::Point q{p.pos.x + x * pitch, p.pos.y + y * pitch};
          const auto code = obs.fixed_code(q, l, hw, 0, p.net);
          row += (x == 0 && y == 0) ? '+' : (code == tmk::route::Obstacles::kFree || code == p.net) ? '.' : '#';
        }
        std::printf("%s\n", row.c_str());
      }
    }
    if (via_map) {  // where a through via of the pad's net (net-class size) passes the fixed-copper checks
      const tmk::Coord drill = std::max(nc.via_drill, rules.minimums.through_hole_diameter);
      const tmk::Coord dia = std::max({nc.via_diameter, rules.minimums.via_diameter, drill + 2 * rules.minimums.via_annular_width});
      std::printf("via %.3f / %.3f mm (V legal)\n", tmk::nm_to_mm(dia), tmk::nm_to_mm(drill));
      for (int l = 0; l < b.copper_count(); ++l)
        std::printf("  centre layer %s: code %d\n", b.copper_name(l).c_str(), static_cast<int>(obs.fixed_code(p.pos, l, dia / 2, 0, p.net, true)));
      std::printf("  hole_to_hole %.3f, hole_clearance %.3f, via code %d\n", tmk::nm_to_mm(rules.minimums.hole_to_hole), tmk::nm_to_mm(rules.minimums.hole_clearance),
                  static_cast<int>(obs.fixed_via_code(p.pos, dia, drill, 0, p.net)));
      for (int y = -n; y <= n; ++y) {
        std::string row;
        for (int x = -n; x <= n; ++x) {
          const tmk::geom::Point q{p.pos.x + x * pitch, p.pos.y + y * pitch};
          const auto code = obs.fixed_via_code(q, dia, drill, 0, p.net);
          row += (x == 0 && y == 0) ? '+' : (code == tmk::route::Obstacles::kFree || code == p.net) ? 'V' : '#';
        }
        std::printf("%s\n", row.c_str());
      }
    }
    return 0;
  }
  std::fprintf(stderr, "pad not found\n");
  return 1;
}

// Differential pairs of a routed board (doc 05 §15): per pair the coupled share of its track length, the gap it
// keeps, the intra-pair skew and vias. Pairs: KiCad's by name, plus any given as "NET_A,NET_B".
int cmd_pairs(const std::string& path, const std::vector<std::string>& extra, const std::string& json_path) {
  auto lb = tmk::io::read_board_file(path);
  const auto rules = tmk::io::read_design_rules(path);
  tmk::model::Board b = lb.board;
  tmk::route::Obstacles obs(b, rules);
  auto pairs = tmk::route::named_pairs(b, obs.rules());
  for (const auto& e : extra) {
    const auto comma = e.find(',');
    if (comma == std::string::npos) throw std::runtime_error("--pair wants NET_A,NET_B: " + e);
    auto id = [&](const std::string& name) {
      for (std::size_t i = 1; i < b.nets.size(); ++i)
        if (b.nets[i].name == name) return static_cast<tmk::model::NetId>(i);
      throw std::runtime_error("no net " + name);
    };
    pairs.emplace_back(id(e.substr(0, comma)), id(e.substr(comma + 1)));
  }
  nlohmann::json j = nlohmann::json::array();
  std::printf("%-28s %9s %9s %8s %9s %9s %8s %5s\n", "pair", "len_mm", "coupled", "gap_mm", "gap_min", "target", "skew_mm", "vias");
  for (const auto& [na, nb] : pairs) {
    const auto pr = tmk::route::pair_rule(b, rules, obs.rules(), na, nb);
    const auto st = tmk::route::measure_pair(b.tracks, b.vias, na, nb, tmk::route::coupled_threshold(pr));
    const std::string name = b.nets[static_cast<std::size_t>(na)].name + " / " + b.nets[static_cast<std::size_t>(nb)].name;
    std::printf("%-28s %9.2f %8.1f%% %8.3f %9.3f %9.3f %8.3f %2d/%-2d\n", name.c_str(), (st.length_a + st.length_b) / 2e6, 100 * st.coupled_share(),
                st.gap_median / 1e6, st.gap_min / 1e6, tmk::nm_to_mm(pr.gap), st.skew() / 1e6, st.vias_a, st.vias_b);
    j.push_back({{"net_a", b.nets[static_cast<std::size_t>(na)].name}, {"net_b", b.nets[static_cast<std::size_t>(nb)].name},
                 {"length_a_mm", st.length_a / 1e6}, {"length_b_mm", st.length_b / 1e6}, {"coupled_share", st.coupled_share()},
                 {"coupled_a_mm", st.coupled_a / 1e6}, {"coupled_b_mm", st.coupled_b / 1e6}, {"gap_median_mm", st.gap_median / 1e6},
                 {"gap_min_mm", st.gap_min / 1e6}, {"target_gap_mm", tmk::nm_to_mm(pr.gap)}, {"width_mm", tmk::nm_to_mm(pr.width)},
                 {"gap_source", pr.source}, {"skew_mm", st.skew() / 1e6}, {"vias_a", st.vias_a}, {"vias_b", st.vias_b}});
  }
  if (!json_path.empty()) std::ofstream(json_path) << nlohmann::json{{"board", path}, {"pairs", j}}.dump(1) << "\n";
  return 0;
}

int cmd_escape(const std::string& path, const std::string& json_path, bool flow, bool soft_zones, double vias_off_pads_below_mm,
               const std::string& project) {
  auto lb = tmk::io::read_board_file(path);
  const auto rules = tmk::io::read_design_rules(path, project);
  tmk::model::Board b = lb.board;
  tmk::route::Obstacles obs(b, rules);
  // The same obstacle semantics as `route` with these options.
  obs.set_soft_zones(soft_zones);
  obs.set_vias_off_pads(vias_off_pads_below_mm > 0, static_cast<tmk::Coord>(vias_off_pads_below_mm * 1e6));
  for (const auto& w : obs.rules().warnings()) std::printf("warning: %s\n", w.c_str());
  const tmk::route::EscapeAnalysisOptions eao;
  const auto parts = tmk::route::analyse_escapes(b, rules, obs, eao);
  nlohmann::json j = nlohmann::json::array();
  int dead = 0, pins = 0, satisfied = 0;
  for (const auto& pe : parts) {
    pins += pe.pins;
    satisfied += pe.satisfied;
    dead += static_cast<int>(pe.dead.size());
    std::printf("%-10s pitch %.3f mm: %d of %d pins escape", pe.ref.c_str(), tmk::nm_to_mm(pe.pitch), pe.escapable, pe.pins);
    if (pe.satisfied > 0) std::printf(" (%d more already connected)", pe.satisfied);
    std::printf("%s\n", pe.dead.empty() ? "" : "; dead:");
    std::map<std::string, std::vector<std::string>> by_reason;
    for (const auto& d : pe.dead) by_reason[d.reason].push_back(b.pads[static_cast<std::size_t>(d.pad)].number);
    nlohmann::json jd = nlohmann::json::array();
    for (const auto& [why, nums] : by_reason) {
      std::string list;
      for (std::size_t k = 0; k < nums.size() && k < 12; ++k) list += (k ? " " : "") + nums[k];
      if (nums.size() > 12) list += " ...";
      std::printf("    %zu pins, %s: %s\n", nums.size(), why.c_str(), list.c_str());
      jd.push_back({{"reason", why}, {"pins", nums}});
    }
    if (!pe.hint.empty()) std::printf("    hint: %s\n", pe.hint.c_str());
    j.push_back({{"ref", pe.ref}, {"pitch_mm", tmk::nm_to_mm(pe.pitch)}, {"pins", pe.pins}, {"escapable", pe.escapable}, {"satisfied", pe.satisfied},
                 {"dead", jd}, {"hint", pe.hint}});
  }
  std::printf("%zu dense packages, %d pins to route, %d cannot escape on the analysis lattice (%.3f mm)%s\n", parts.size(), pins, dead,
              tmk::nm_to_mm(eao.lattice), soft_zones ? ", fills ignored" : "");
  nlohmann::json jf;
  if (flow) {
    // Escape plan v2 at the net classes' rules (the router's strict-pass width, clearance and class via).
    std::map<tmk::model::NetId, int> on_net;
    for (const auto& p : b.pads)
      if (p.net > 0) ++on_net[p.net];
    std::vector<char> needs(b.pads.size(), 0);
    for (std::size_t i = 0; i < b.pads.size(); ++i) needs[i] = b.pads[i].net > 0 && on_net[b.pads[i].net] > 1;
    auto nc = [&](tmk::model::NetId n) -> const tmk::model::NetClass& { return rules.class_for(b.nets[static_cast<std::size_t>(n)].name); };
    auto width = [&](tmk::model::NetId n) { return std::max(nc(n).track_width, rules.minimums.track_width); };
    auto via_d = [&](tmk::model::NetId n) {
      const tmk::Coord drill = std::max(nc(n).via_drill, rules.minimums.through_hole_diameter);
      return std::max({nc(n).via_diameter, rules.minimums.via_diameter, drill + 2 * rules.minimums.via_annular_width});
    };
    auto via_drill = [&](tmk::model::NetId n) { return std::max(nc(n).via_drill, rules.minimums.through_hole_diameter); };
    auto ok = [](std::int32_t c, tmk::model::NetId n) { return c == tmk::route::Obstacles::kFree || c == static_cast<std::int32_t>(n); };
    tmk::route::FlowEscapeInput in;
    in.width = width;
    in.clearance = [&](tmk::model::NetId n) { return std::max(nc(n).clearance, rules.minimums.clearance); };
    in.via = via_d;
    in.keep = [&](tmk::model::NetId n) { return width(n) + std::max(nc(n).clearance, rules.minimums.clearance); };
    // The router's custom-rule limits, as in its own escape planning (doc 05 §34).
    const auto& re = obs.rules();
    in.track_free = [&](int l, tmk::geom::Point p, tmk::model::NetId n) {
      return re.track_allowed(n, l) && ok(obs.fixed_code(p, l, width(n) / 2, 0, n), n) &&
             !obs.track_disallowed(tmk::geom::Shape::point(p, width(n) / 2), l, width(n), n);
    };
    in.via_free = [&](tmk::geom::Point p, tmk::model::NetId n) {
      return re.via_allowed(n) && !re.via_hole_rule_hits_own_tracks(n) && ok(obs.fixed_via_code(p, via_d(n), via_drill(n), 0, n), n) &&
             !obs.via_disallowed(p, via_d(n), n, 0, b.copper_count() - 1, tmk::model::ViaType::Through);
    };
    in.layers = b.copper_count();
    tmk::route::FlowEscapeStats fs;
    tmk::route::plan_escapes_flow(b, needs, in, {}, &fs);
    std::printf("escape plan v2: %d deep arrays\n", fs.arrays);
    jf = {{"arrays", fs.arrays}, {"rings", nlohmann::json::array()}, {"per_layer", fs.per_layer}};
    for (std::size_t r = 0; r < fs.rings.size(); ++r) {
      const auto& rp = fs.rings[r];
      std::printf("  ring %2zu: %4d pins: %4d pad layer, %4d via + other layer, %4d via only, %4d none\n", r + 1, rp.pins, rp.pad_layer, rp.other_layer,
                  rp.via_only, rp.none);
      jf["rings"].push_back({{"ring", r + 1}, {"pins", rp.pins}, {"pad_layer", rp.pad_layer}, {"other_layer", rp.other_layer}, {"via_only", rp.via_only}, {"none", rp.none}});
    }
    std::printf("  escapes per layer:");
    for (std::size_t l = 0; l < fs.per_layer.size(); ++l) std::printf(" %s %d", b.copper_name(static_cast<int>(l)).c_str(), fs.per_layer[l]);
    std::printf("\n");
  }
  if (!json_path.empty()) {
    nlohmann::json out{{"board", path},
                       {"parts", j},
                       {"pins", pins},
                       {"dead", dead},
                       {"satisfied", satisfied},
                       {"domain", {{"lattice_mm", tmk::nm_to_mm(eao.lattice)}, {"margin_mm", tmk::nm_to_mm(eao.margin)}, {"window_mm", tmk::nm_to_mm(eao.window)},
                                   {"soft_zones", soft_zones}, {"vias_off_pads_below_mm", vias_off_pads_below_mm}}}};
    if (flow) out["flow"] = jf;
    std::ofstream(json_path) << out.dump(1) << "\n";
  }
  return 0;
}

int cmd_debug_seg(const std::string& path, const std::vector<double>& v, int layer, double width_mm, const std::string& netname) {
  auto lb = tmk::io::read_board_file(path);
  const auto rules = tmk::io::read_design_rules(path);
  tmk::model::Board b = lb.board;
  tmk::route::Obstacles obs(b, rules);
  tmk::model::NetId net = 0;
  for (std::size_t i = 0; i < b.nets.size(); ++i)
    if (b.nets[i].name == netname) net = static_cast<tmk::model::NetId>(i);
  const tmk::geom::Point a{static_cast<tmk::Coord>(v[0] * 1e6), static_cast<tmk::Coord>(v[1] * 1e6)};
  const tmk::geom::Point e{static_cast<tmk::Coord>(v[2] * 1e6), static_cast<tmk::Coord>(v[3] * 1e6)};
  const tmk::Coord w = static_cast<tmk::Coord>(width_mm * 1e6);
  std::printf("segment_state %d (net %d)\n", obs.segment_state(a, e, layer, w, net, true, nullptr), static_cast<int>(net));
  const auto s = tmk::geom::Shape::segment(a, e, w / 2);
  for (const auto& it : obs.copper().items) {
    if (it.removed || !(it.layers & tmk::model::layer_bit(layer))) continue;
    for (const auto& sh : it.shapes) {
      const double g = tmk::geom::gap(s, sh);
      if (g < 1.0e6) std::printf("  item kind %d net %d gap %.4f mm\n", static_cast<int>(it.kind), static_cast<int>(it.net), g / 1e6);
    }
  }
  for (const auto& h : obs.copper().holes) {
    const double g = tmk::geom::gap(s, h.shape);
    if (g < 1.0e6) std::printf("  hole plated %d net %d clearance %.4f gap %.4f mm\n", h.plated ? 1 : 0, static_cast<int>(h.net), tmk::nm_to_mm(h.clearance), g / 1e6);
  }
  return 0;
}

}  // namespace

int main(int argc, char** argv) {
  CLI::App app{"TraceMaker: placement-aware PCB autorouter for KiCad"};
  app.require_subcommand(1);
  auto* version = app.add_subcommand("version", "Print the version");
  auto* gpu_info = app.add_subcommand("gpu-info", "List GPU devices and their available memory budget");

  auto* inspect = app.add_subcommand("inspect", "Read a .kicad_pcb and print a summary");
  std::string inspect_path, inspect_json;
  std::string inspect_project;
  inspect->add_option("--project", inspect_project, "Read this project and its same-stem .kicad_dru");
  inspect->add_option("board", inspect_path, "Board file")->required()->check(CLI::ExistingFile);
  inspect->add_option("--json", inspect_json, "Also write the board as JSON (KiCad truth schema)");

  auto* crules_cmd = app.add_subcommand("rules", "Detect component categories and report the component-aware layout rules (doc 15)");
  std::string cr_path, cr_json, cr_dru, cr_cat, cr_roles_from, cr_override, cr_mode = "soft";
  std::string cr_project;
  crules_cmd->add_option("--project", cr_project, "Read this project and its same-stem .kicad_dru");
  crules_cmd->add_option("board", cr_path, "Board file")->required()->check(CLI::ExistingFile);
  crules_cmd->add_option("--json", cr_json, "Write the full report (every rule of every instance) as JSON");
  crules_cmd->add_option("--mode", cr_mode, "Which statuses to report: report (check only), soft (as placement would apply them), on (with keep-outs)")
      ->check(CLI::IsMember({"report", "soft", "on"}));
  crules_cmd->add_option("--dru", cr_dru, "Write the generated custom rules (sidecar .kicad_dru) to this file");
  crules_cmd->add_option("--roles-from", cr_roles_from, "Detect and bind roles on this version of the board (e.g. the placement input), measure on the given one");
  crules_cmd->add_option("--catalogue", cr_cat, "Catalogue JSON to use instead of the built-in one (scripts/crules_catalogue.py output)");
  crules_cmd->add_option("--rules-override", cr_override,
                         "User override file (JSON, doc 15 §6.3): disable rules, assert/deny categories, set parameters")
      ->check(CLI::ExistingFile);

  auto* selftest = app.add_subcommand("selftest-edit", "Apply a fixed set of edits (for integration tests)");
  selftest->group("");  // hidden
  std::string st_in, st_out;
  selftest->add_option("in", st_in)->required();
  selftest->add_option("out", st_out)->required();

  auto* rt = app.add_subcommand("check-roundtrip", "Parse and re-write boards; report any that do not round-trip byte for byte");
  rt->group("");
  std::vector<std::string> rt_files;
  rt->add_option("boards", rt_files)->required();

  auto* drc = app.add_subcommand("drc", "Check a board against its KiCad design rules");
  std::string drc_path, drc_json;
  std::string drc_project;
  drc->add_option("--project", drc_project, "Read this project and its same-stem .kicad_dru");
  drc->footer("Exit codes: 0 no reported violations or unconnected items; 5 violations or unconnected items; "
              "1 runtime error. CLI usage errors return CLI11's nonzero usage status. Not KiCad DRC sign-off.");
  double drc_eps_um = 0.5;
  drc->add_option("board", drc_path)->required()->check(CLI::ExistingFile);
  drc->add_option("--json", drc_json, "Write the report as JSON (kicad-cli layout)");
  drc->add_option("--epsilon-um", drc_eps_um, "Tolerance below the required clearance, micrometres");
  bool drc_linear_zones = false;
  drc->add_flag("--linear-zones", drc_linear_zones, "Reference path: test items against every edge of a zone fill")->group("");

  auto* pert = app.add_subcommand("selftest-perturb", "Add random tracks/vias and footprint moves (DRC parity fuzzing)");
  pert->group("");
  std::string pin, pout;
  std::uint64_t pseed = 1;
  int ptracks = 30, pvias = 20, pmoves = 5;
  pert->add_option("in", pin)->required();
  pert->add_option("out", pout)->required();
  pert->add_option("--seed", pseed);
  pert->add_option("--tracks", ptracks);
  pert->add_option("--vias", pvias);
  pert->add_option("--moves", pmoves);

  auto* brk = app.add_subcommand("selftest-defects", "Inject one kind of defect into a routed board (broken-board DRC parity)");
  brk->group("");
  std::string bk_in, bk_out, bk_manifest, bk_kind;
  std::uint64_t bk_seed = 1;
  int bk_count = 3;
  brk->add_option("in", bk_in)->required()->check(CLI::ExistingFile);
  brk->add_option("out", bk_out)->required();
  brk->add_option("--kind", bk_kind)->required()->check(CLI::IsMember(tmk::app::defect_kinds()));
  brk->add_option("--seed", bk_seed);
  brk->add_option("--count", bk_count);
  brk->add_option("--manifest", bk_manifest, "Write the injected defects as JSON");

  auto* route = app.add_subcommand("route", "Route all unrouted connections of a board");

  tmk::app::RouteJob r_job;
  tmk::app::add_route_options(*route, r_job, true);

  auto* esc = app.add_subcommand("escape", "Escape feasibility of dense packages: pins that cannot leave their package under the board's rules");
  std::string esc_board, esc_json;
  std::string esc_project;
  esc->add_option("--project", esc_project, "Read this project and its same-stem .kicad_dru");
  esc->add_option("board", esc_board)->required()->check(CLI::ExistingFile);
  esc->add_option("--json", esc_json, "Write the analysis as JSON");
  bool esc_flow = false, esc_soft = false, esc_vop = false;
  double esc_vop_below = 2.0;
  esc->add_flag("--flow", esc_flow, "Also plan deep BGA arrays by min-cost flow and report the channel/layer assignment per ring");
  esc->add_flag("--soft-zones", esc_soft, "Zone fills do not block (as route --soft-zones): analyse what a refill would leave");
  esc->add_flag("--keep-vias-off-pads", esc_vop, "Vias keep clear of SMD pads narrower than --vias-off-pads-below (as route)");
  esc->add_option("--vias-off-pads-below", esc_vop_below, "Pad width (mm) below which --keep-vias-off-pads applies (default 2)");
  auto* pairs_cmd = app.add_subcommand("pairs", "Differential pairs of a routed board: coupled share, gap, intra-pair skew");
  std::string pr_board, pr_json;
  std::vector<std::string> pr_extra;
  pairs_cmd->add_option("board", pr_board)->required()->check(CLI::ExistingFile);
  pairs_cmd->add_option("--pair", pr_extra, "Also measure this pair (NET_A,NET_B), e.g. USB nets named DP/DM");
  pairs_cmd->add_option("--json", pr_json, "Write the measurements as JSON");
  auto* dseg = app.add_subcommand("debug-seg", "Explain the router's verdict on one segment");
  dseg->group("");
  std::string ds_board, ds_net;
  std::vector<double> ds_pts;
  int ds_layer = 0;
  double ds_width = 0.25;
  dseg->add_option("board", ds_board)->required();
  dseg->add_option("--pts", ds_pts)->expected(4)->required();
  dseg->add_option("--layer", ds_layer);
  dseg->add_option("--width", ds_width);
  dseg->add_option("--net", ds_net);
  auto* dbg = app.add_subcommand("debug-pad", "Print the router's legality map around a pad");
  dbg->group("");
  std::string d_board, d_ref, d_num;
  double d_pitch = 0.08, d_radius = 2.0, d_width = 0;
  bool d_via = false;
  dbg->add_option("board", d_board)->required();
  dbg->add_option("ref", d_ref)->required();
  dbg->add_option("pad", d_num)->required();
  dbg->add_option("--pitch", d_pitch);
  dbg->add_option("--radius", d_radius);
  dbg->add_option("--width", d_width);
  dbg->add_flag("--via", d_via, "Also print where a via of the pad's net is legal");

  CLI11_PARSE(app, argc, argv);
  try {
    if (*version) {
      std::printf("tracemaker %s\n", std::string(tmk::version()).c_str());
      return 0;
    }
    if (*gpu_info) return cmd_gpu_info();
    if (*inspect) {
      const auto lb = tmk::io::read_board_file(inspect_path);
      tmk::app::print_summary(lb.board, inspect_path);
      if (!inspect_json.empty()) {
        const auto rules = tmk::io::read_design_rules(inspect_path, inspect_project);
        tmk::app::write_truth_json(lb.board, inspect_json, &rules);
      }
      return 0;
    }
    if (*crules_cmd) return cmd_rules(cr_path, cr_mode, cr_json, cr_dru, cr_roles_from, cr_cat, cr_override, cr_project);
    if (*selftest) return cmd_selftest_edit(st_in, st_out);
    if (*dseg) return cmd_debug_seg(ds_board, ds_pts, ds_layer, ds_width, ds_net);
    if (*esc) return cmd_escape(esc_board, esc_json, esc_flow, esc_soft, esc_vop ? esc_vop_below : 0.0, esc_project);
    if (*pairs_cmd) return cmd_pairs(pr_board, pr_extra, pr_json);
    if (*dbg) return cmd_debug_pad(d_board, d_ref, d_num, d_pitch, d_radius, d_width, d_via);
    if (*drc) return cmd_drc(drc_path, drc_json, static_cast<tmk::Coord>(drc_eps_um * 1000.0), drc_linear_zones, drc_project);
    if (*pert) return cmd_perturb(pin, pout, pseed, ptracks, pvias, pmoves);
    if (*brk) {
      const int n = tmk::app::inject_defects(bk_in, bk_out, bk_manifest, bk_kind, bk_seed, bk_count);
      std::printf("%d %s defects injected\n", n, bk_kind.c_str());
      return 0;
    }
    if (*route) {
      return cmd_route(std::move(r_job));
    }
    if (*rt) {
      int bad = 0, ok = 0;
      for (const auto& f : rt_files) {
        try {
          const auto lb = tmk::io::read_board_file(f);
          if (lb.doc.write() != lb.doc.text()) throw std::runtime_error("write differs from input");
          if (!lb.board.warnings.empty()) std::printf("WARN %s: %s\n", f.c_str(), lb.board.warnings.front().c_str());
          ++ok;
        } catch (const std::exception& e) {
          ++bad;
          std::printf("FAIL %s: %s\n", f.c_str(), e.what());
        }
      }
      std::printf("%d ok, %d failed\n", ok, bad);
      return bad ? 1 : 0;
    }
  } catch (const std::exception& e) {
    std::fprintf(stderr, "error: %s\n", e.what());
    return 1;
  }
  return 1;
}
