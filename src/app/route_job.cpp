// SPDX-License-Identifier: GPL-3.0-or-later
#include "app/route_job.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <map>
#include <memory>
#include <mutex>
#include <set>
#include <sstream>
#include <stdexcept>
#include <thread>

#include "crules/engine.hpp"
#include "crules/intent.hpp"
#include "crules/names.hpp"
#include "drc/copper.hpp"
#include "drc/drc.hpp"
#include "drc/refill.hpp"
#include "drc/rule_engine.hpp"
#include "route/diff_pair.hpp"
#include "route/layer_limits.hpp"
#include "gpu/device.hpp"
#include "io/kicad/board_editor.hpp"
#include "io/kicad/board_reader.hpp"
#include "io/kicad/project_reader.hpp"
#include "learn/knowledge_base.hpp"
#include "passes/stitch.hpp"
#include "passes/style.hpp"
#include "passes/widen.hpp"
#include "server/messages.hpp"
#include "server/replay_log.hpp"
#include "server/viewer_server.hpp"

namespace tmk::app {
namespace {

__attribute__((format(printf, 1, 2))) std::string fmt(const char* f, ...) {
  char buf[1024];
  va_list ap;
  va_start(ap, f);
  const int n = std::vsnprintf(buf, sizeof buf, f, ap);
  va_end(ap);
  if (n < 0) return {};
  if (static_cast<std::size_t>(n) < sizeof buf) return std::string(buf, static_cast<std::size_t>(n));
  std::string s(static_cast<std::size_t>(n) + 1, '\0');
  va_start(ap, f);
  std::vsnprintf(s.data(), s.size(), f, ap);
  va_end(ap);
  s.resize(static_cast<std::size_t>(n));
  return s;
}

// Records router events (JSON lines with a time stamp) for replay, e.g. comparison videos. A path ending in
// ".zst" is written zstd-compressed (server/replay_log.hpp, decision D45).
class FileSink final : public events::Sink {
 public:
  explicit FileSink(const std::string& path) : w_(path), t0_(std::chrono::steady_clock::now()) {}
  void publish(std::string json) override {
    if (json.size() < 2 || json.front() != '{') return;
    const bool stamped = json.rfind("{\"t\":", 0) == 0;  // already time-stamped (buffered portfolio replay)
    const bool keyframe = !stamped && server::message_type(json) == "board";  // snapshots are never buffered
    std::lock_guard<std::mutex> lk(m_);
    if (stamped) {
      w_.write_line(json);
    } else {
      std::ostringstream t;
      t << std::chrono::duration<double>(std::chrono::steady_clock::now() - t0_).count();
      w_.write_line("{\"t\":" + t.str() + "," + json.substr(1));
    }
    if (keyframe) w_.end_frame();  // the snapshot is a frame of its own: a reader can start from it
  }
  bool wants_transient() const override { return false; }

 private:
  server::ReplayWriter w_;
  std::chrono::steady_clock::time_point t0_;
  std::mutex m_;
};

nlohmann::json items_json(const model::Board& b, const route::RouteResult& res) {
  // New copper for the KiCad plugin: layer and net by name, coordinates in nm.
  nlohmann::json it{{"tracks", nlohmann::json::array()}, {"vias", nlohmann::json::array()}};
  for (const auto& t : res.tracks)
    it["tracks"].push_back({{"start", {t.a.x, t.a.y}}, {"end", {t.b.x, t.b.y}}, {"width", t.width}, {"layer", b.copper_name(t.layer)},
                            {"net", b.nets[static_cast<std::size_t>(t.net)].name}});
  for (const auto& v : res.vias)
    it["vias"].push_back({{"position", {v.pos.x, v.pos.y}}, {"diameter", v.size}, {"drill", v.drill}, {"top", b.copper_name(v.layer_top)},
                          {"bottom", b.copper_name(v.layer_bottom)}, {"net", b.nets[static_cast<std::size_t>(v.net)].name}});
  return it;
}

// Work profile (doc 10 §2, D88), per phase and in total.
nlohmann::json work_json(const route::WorkProfile& w) {
  auto counts = [](const route::WorkCounts& c) {
    return nlohmann::json{{"expansions", c.expansions}, {"flood_cells", c.flood_cells}, {"searches", c.searches},
                          {"cell_checks", c.cell_checks}, {"fixed_checks", c.fixed_checks}, {"field_cells", c.field_cells},
                          {"fields", c.fields},         {"commits", c.commits},         {"rips", c.rips}};
  };
  nlohmann::json j;
  for (int p = 0; p < route::kWorkPhases; ++p) j[route::work_phase_name(static_cast<route::WorkPhase>(p))] = counts(w.phase[static_cast<std::size_t>(p)]);
  j["total"] = counts(w.total());
  return j;
}

}  // namespace

std::vector<route::BusGroup> resolve_buses(const model::Board& b, const model::DesignRules& rules, const std::vector<std::string>& specs,
                                           bool detect, double skew_mm, std::vector<std::string>* notes) {
  std::vector<route::BusGroup> out;
  std::vector<int> owner(b.nets.size(), -1);
  auto claim = [&](const route::BusGroup& g) {
    for (const auto n : g.members)
      if (owner[static_cast<std::size_t>(n)] < 0) owner[static_cast<std::size_t>(n)] = static_cast<int>(out.size());
  };
  for (const auto& spec : specs) {
    auto g = route::bus_from_option(b, spec);
    for (const auto n : g.members)
      if (owner[static_cast<std::size_t>(n)] >= 0 && notes)
        notes->push_back("warning: net " + b.nets[static_cast<std::size_t>(n)].name + " is in bus " + out[static_cast<std::size_t>(owner[static_cast<std::size_t>(n)])].name +
                         " and " + g.name + "; it is routed with " + out[static_cast<std::size_t>(owner[static_cast<std::size_t>(n)])].name);
    claim(g);
    out.push_back(std::move(g));
  }
  if (detect)
    for (auto& g : route::detect_buses(b)) {
      if (std::any_of(g.members.begin(), g.members.end(), [&](model::NetId n) { return owner[static_cast<std::size_t>(n)] >= 0; })) continue;
      claim(g);
      out.push_back(std::move(g));
    }
  const drc::RuleEngine re(b, rules);
  for (auto& g : out) {
    if (const auto s = route::bus_rule_skew(re, g)) {
      g.skew_limit = *s;
      g.skew_source = "rule";
    } else if (g.source == "option" && skew_mm > 0) {
      g.skew_limit = static_cast<Coord>(skew_mm * 1e6);
      g.skew_source = "option";
    }
  }
  return out;
}

nlohmann::json bus_report(const model::Board& b, const model::DesignRules& rules, const std::vector<std::uint8_t>& joined, const route::BusGroup& g,
                          std::string* line) {
  const auto st = route::measure_bus(b, route::via_heights(b, rules), b.tracks, b.arcs, b.vias, joined, g);
  auto layer_names = [&](model::LayerMask m) {
    nlohmann::json a = nlohmann::json::array();
    for (int l = 0; l < b.copper_count(); ++l)
      if (m & model::layer_bit(l)) a.push_back(b.copper_name(l));
    return a;
  };
  nlohmann::json members = nlohmann::json::array();
  for (const auto& m : st.members)
    members.push_back({{"net", b.nets[static_cast<std::size_t>(m.net)].name}, {"routed", m.routed}, {"followed", m.followed},
                       {"length_mm", m.length / 1e6}, {"via_length_mm", m.via_length / 1e6}, {"vias", m.vias}, {"layers", layer_names(m.layers)}});
  nlohmann::json j{{"name", g.name}, {"source", g.source}, {"members", members}, {"nets", st.members.size()}, {"routed", st.routed},
                   {"followed", st.followed}, {"left", st.left}, {"reference_vias", st.ref_vias}, {"reference_layers", layer_names(st.ref_layers)},
                   {"vias_min", st.vias_min}, {"vias_max", st.vias_max}, {"length_min_mm", st.length_min / 1e6},
                   {"length_max_mm", st.length_max / 1e6}, {"skew_mm", st.skew() / 1e6}};
  // Within the limit only when every member is routed: an unrouted member has no length to compare.
  const bool all = st.routed == static_cast<int>(st.members.size());
  if (g.skew_limit > 0) {
    j["skew_limit_mm"] = nm_to_mm(g.skew_limit);
    j["skew_source"] = g.skew_source;
    j["within_skew"] = all && st.skew() <= static_cast<double>(g.skew_limit);
  } else {
    j["skew_limit_mm"] = nullptr;
    j["within_skew"] = nullptr;
  }
  if (line) {
    *line = fmt("%s (%s): %d/%zu nets routed, %d on the bus's layers, %d left the bus, vias %d-%d, length %.2f-%.2f mm, skew %.3f mm", g.name.c_str(),
                g.source.c_str(), st.routed, st.members.size(), st.followed, st.left, st.vias_min, st.vias_max, st.length_min / 1e6, st.length_max / 1e6,
                st.skew() / 1e6);
    if (g.skew_limit > 0)
      *line += fmt(" (limit %.3f mm from the %s: %s)", nm_to_mm(g.skew_limit), g.skew_source.c_str(),
                   !all ? "not all routed" : st.skew() <= static_cast<double>(g.skew_limit) ? "within" : "OVER");
  }
  return j;
}

int default_gpu_device(bool use_gpu) {
  if (!use_gpu) return -1;
  const auto devs = gpu::list_devices();
  return devs.empty() ? -1 : devs.front().index;
}

RouteJob::RouteJob() : kb_path(learn::KnowledgeBase::default_path()) {}


RouteJobResult run_route_job(RouteJob job) {
  const auto t_job = std::chrono::steady_clock::now();
  auto log = [&](const std::string& line) {
    if (job.log) job.log(line);
  };
  auto& opt = job.opt;
  if (!job.guide.empty()) {
    // Guide coordinates are Simple Route JSON millimetres, y up (doc 16 F1); board coordinates are y down.
    const auto gj = nlohmann::json::parse(std::ifstream(job.guide));
    auto pt = [](double x, double y) { return geom::Point{mm_to_nm(x), -mm_to_nm(y)}; };
    for (const auto& g : gj) {
      route::RouterOptions::GuidePath gp;
      gp.a = pt(g["a"][0].get<double>(), g["a"][1].get<double>());
      gp.b = pt(g["b"][0].get<double>(), g["b"][1].get<double>());
      for (const auto& sg : g["segs"]) {
        gp.segs.push_back({pt(sg[0].get<double>(), sg[1].get<double>()), pt(sg[2].get<double>(), sg[3].get<double>())});
        gp.seg_layer.push_back(sg[4].get<int>());
      }
      for (const auto& v : g["vias"]) gp.vias.push_back(pt(v[0].get<double>(), v[1].get<double>()));
      opt.guides.push_back(std::move(gp));
    }
  }
  auto lb = io::read_board_file(job.in);
  const auto sources = io::design_rule_sources(job.in, job.project);
  auto rules = io::read_design_rules(job.in, job.project);  // component rules may add net classes in memory
  std::vector<std::string> rule_warnings = rules.warnings;
  log("rules project: " + sources.project + (std::filesystem::exists(sources.project) ? " (loaded)" : " (absent; board/default rules)"));
  log("rules dru: " + sources.dru + (std::filesystem::exists(sources.dru) ? " (loaded)" : " (absent)"));
  for (const auto& warning : rule_warnings) log("warning: " + warning);
  std::vector<bool> reroute_nets(job.reroute_nets.empty() ? 0 : lb.board.nets.size(), false);
  for (const auto& name : job.reroute_nets) {
    const auto it = std::find_if(lb.board.nets.begin(), lb.board.nets.end(), [&](const auto& net) { return net.name == name; });
    if (it == lb.board.nets.end()) throw std::invalid_argument("--reroute-nets: unknown net '" + name + "'");
    reroute_nets[static_cast<std::size_t>(it - lb.board.nets.begin())] = true;
  }
  if (job.reroute || !job.reroute_nets.empty()) {
    // Strip the unlocked tracks and vias from the document and read it again (the editor changes the document,
    // not the model).
    int removed = 0;
    {
      io::BoardEditor ed(lb, opt.seed);
      for (std::size_t i = 0; i < lb.board.tracks.size(); ++i)
        if (!lb.board.tracks[i].locked && (job.reroute || reroute_nets[static_cast<std::size_t>(lb.board.tracks[i].net)]))
          ed.remove_track(i), ++removed;
      for (std::size_t i = 0; i < lb.board.vias.size(); ++i)
        if (!lb.board.vias[i].locked && (job.reroute || reroute_nets[static_cast<std::size_t>(lb.board.vias[i].net)]))
          ed.remove_via(i), ++removed;
      if (!job.reroute_nets.empty())
        for (const auto& arc : lb.board.arcs)
          if (!arc.locked && reroute_nets[static_cast<std::size_t>(arc.net)]) lb.doc.remove(arc.node), ++removed;
      const std::string tmp = (job.out.empty() ? (std::filesystem::temp_directory_path() / "tracemaker-reroute.kicad_pcb").string() : job.out + ".reroute-tmp.kicad_pcb");
      ed.save(tmp);
      lb = io::read_board_file(tmp);
      std::filesystem::remove(tmp);
    }
    log(fmt("reroute: %d unlocked copper items removed%s", removed, job.reroute ? "" : " from selected nets"));
  }
  std::vector<route::ProtectedPlane> protected_planes;
  if (job.protect_planes || !job.no_tracks_on.empty() || !job.layer_costs.empty()) {
    const auto& bd = lb.board;
    auto lim = route::layer_limits(bd, job.no_tracks_on, job.layer_costs, job.protect_planes);
    protected_planes = std::move(lim.protected_planes);
    if (job.protect_planes) {
      std::string planes;
      for (const auto& plane : protected_planes)
        planes += (planes.empty() ? "" : ", ") +
                  fmt("%s (%s, %.1f%%)", bd.copper_name(plane.layer).c_str(),
                      bd.nets[static_cast<std::size_t>(plane.net)].name.c_str(), plane.coverage * 100);
      log("protected planes: " + (planes.empty() ? std::string("none") : planes));
    }
    for (const auto& warning : lim.warnings) {
      log("warning: " + warning);
      rule_warnings.push_back(warning);
    }
    opt.no_track_layers = lim.no_track_layers;
    opt.layer_cost_pm = lim.layer_cost_pm;
    std::string names;
    int stranded = 0;  // pads with a net whose copper lies only on layers without tracks
    for (int l = 0; l < bd.copper_count(); ++l)
      if (lim.no_track_layers & model::layer_bit(l)) names += (names.empty() ? "" : ", ") + bd.copper_name(l);
    for (const auto& p : bd.pads) stranded += p.net != 0 && p.copper != 0 && (p.copper & ~lim.no_track_layers) == 0;
    if (!names.empty()) log("no new tracks on: " + names);
    for (int l = 0; l < bd.copper_count() && !lim.layer_cost_pm.empty(); ++l)
      if (lim.layer_cost_pm[static_cast<std::size_t>(l)] != 1000)
        log(fmt("track cost on %s: x%.3g", bd.copper_name(l).c_str(), lim.layer_cost_pm[static_cast<std::size_t>(l)] / 1000.0));
    if (stranded) log(fmt("warning: %d pads lie only on layers that take no tracks; their connections stay unrouted", stranded));
  }
  if (opt.via_in_pad && (rules.minimums.via_diameter <= 0 || rules.minimums.through_hole_diameter <= 0))
    log("warning: --via-in-pad: the board's minimum via diameter or drill could not be read, so no via in pad is placed");
  const std::string name = std::filesystem::path(job.in).filename().string();
  RouteJobResult out;
  std::unique_ptr<server::ViewerServer> server;
  if (job.view) {
    server::ServerOptions so;
    so.host = job.view_host;
    so.port = static_cast<std::uint16_t>(job.view_port);
    server = std::make_unique<server::ViewerServer>(so);
    server->publish(server::board_snapshot_json(lb.board, name));
    out.viewer_url = server->url();
    log("live view: " + out.viewer_url);
    opt.sink = server.get();
  }
  std::unique_ptr<FileSink> recorder;
  if (!job.record.empty() && !job.view) {
    recorder = std::make_unique<FileSink>(job.record);
    recorder->publish(server::board_snapshot_json(lb.board, name));
    opt.sink = recorder.get();
    opt.buffer_events = true;
  }
  // Knowledge base (T3, D106): report history, never spend routing work on an unproven order.
  std::unique_ptr<learn::KnowledgeBase> kb;
  if (!job.kb_path.empty()) {
    kb = std::make_unique<learn::KnowledgeBase>(job.kb_path);
    if (!kb->ok()) kb.reset();
  }
  int conn_estimate = 0;
  for (const auto& n : lb.board.nets) conn_estimate += !n.name.empty();
  const auto feat = learn::features_of(lb.board, lb.doc.text(), conn_estimate);
  if (kb) {
    const int previous = kb->failed_connection_count(feat.hash);
    if (previous > 0) log(fmt("knowledge base: %d connections left unrouted before (reporting only; routing order unchanged)", previous));
  }
  // Component rules (doc 15 §5.5): generated keep-outs are added to an in-memory copy of the board the router
  // sees; the output is written from the original document, so the user's board never gains them.
  const crules::Mode cr_mode = crules::parse_mode(job.component_rules);
  // After-route passes (doc 05 §39): what the options and the component rules ask of them.
  if (!job.stitch_vias.empty() && job.stitch_vias != "planes" && job.stitch_vias != "return" && job.stitch_vias != "all")
    throw std::invalid_argument("--stitch-vias: expected planes, return or all, got '" + job.stitch_vias + "'");
  const bool stitch_planes = job.stitch_vias == "planes" || job.stitch_vias == "all";
  const bool stitch_return = job.stitch_vias == "return" || job.stitch_vias == "all";
  std::vector<passes::WidenOptions::Target> widen_targets;
  std::vector<passes::StitchOptions::ReturnNet> stitch_returns;
  // "PATTERN=VALUE": the nets whose name matches the pattern (KiCad wildcards), and the text after the last '='.
  auto pattern_nets = [&](const std::string& opt_name, const std::string& entry, std::string& value) {
    const auto eq = entry.rfind('=');
    if (eq == std::string::npos || eq == 0 || eq + 1 == entry.size()) throw std::invalid_argument(opt_name + ": expected PATTERN=VALUE, got '" + entry + "'");
    value = entry.substr(eq + 1);
    std::vector<model::NetId> nets;
    for (const auto& n : lb.board.nets)
      if (n.id > 0 && !n.name.empty() && model::wildcard_match(entry.substr(0, eq), n.name)) nets.push_back(n.id);
    if (nets.empty()) log("warning: " + opt_name + " " + entry + " matches no net");
    return nets;
  };
  for (const auto& e : job.widen_nets) {
    std::string v;
    const auto nets = pattern_nets("--widen-net", e, v);
    char* end = nullptr;
    const double mm = std::strtod(v.c_str(), &end);
    if (end == v.c_str() || *end != '\0' || !(mm > 0) || mm > 100) throw std::invalid_argument("--widen-net: bad width in '" + e + "' (millimetres)");
    for (const auto n : nets) widen_targets.push_back({n, static_cast<Coord>(std::llround(mm * 1e6)), "option"});
  }
  if (stitch_return)
    for (const auto& e : job.stitch_returns) {
      std::string v;
      const auto nets = pattern_nets("--stitch-return", e, v);
      const model::NetId plane = lb.board.net_by_name(v);
      if (plane == 0) throw std::invalid_argument("--stitch-return: no net named '" + v + "'");
      const double mm = job.stitch_distance_mm > 0 ? job.stitch_distance_mm : crules::builtin_catalogue().stitch_via_within_mm;
      for (const auto n : nets)
        if (n != plane) stitch_returns.push_back({n, plane, static_cast<Coord>(mm * 1e6), "option"});
    }
  model::Board with_rules;
  nlohmann::json cr_classes = nlohmann::json::array();
  const model::Board* route_board = &lb.board;
  if (cr_mode == crules::Mode::Off && !job.rules_override.empty()) {
    crules::load_overrides_file(job.rules_override, crules::builtin_catalogue());  // still validated: a broken file is an error
    log("warning: --rules-override " + job.rules_override + " has no effect with --component-rules off");
  }
  if (cr_mode != crules::Mode::Off) {
    const auto& cat = crules::builtin_catalogue();
    crules::Overrides ov;
    if (!job.rules_override.empty()) ov = crules::load_overrides_file(job.rules_override, cat);
    const auto det = crules::detect(lb.board, cat, job.rules_override.empty() ? nullptr : &ov);
    if (!job.rules_override.empty()) {
      log(fmt("component rules: %zu user override(s) from %s", det.override_entries.size(), job.rules_override.c_str()));
      for (const auto& u : det.override_unused) log("  warning: override matched nothing: " + u);
    }
    std::vector<std::string> skipped;
    const auto kos = crules::generate_keepouts(lb.board, cat, det, &skipped);
    log(fmt("component rules (%s): %zu instance(s), %zu generated keep-out(s)%s", job.component_rules.c_str(), det.instances.size(), kos.size(),
            cr_mode == crules::Mode::On ? "" : " (not applied: use --component-rules on)"));
    for (const auto& s : skipped) log("  keep-out not generated: " + s);
    // Events for the viewer and recordings (rule 7, doc 15 §7): what was detected and the keep-out polygons (nm).
    if (opt.sink) {
      for (const auto& in : det.instances)
        opt.sink->publish(nlohmann::json{{"type", "crules.detected"},
                                         {"category", cat.categories[static_cast<std::size_t>(in.category)].id},
                                         {"anchor", lb.board.footprints[static_cast<std::size_t>(in.anchor)].reference},
                                         {"confidence", in.confidence}}
                              .dump());
      for (const auto& k : kos) {
        nlohmann::json poly = nlohmann::json::array();
        for (const auto& q : k.zone.outline.front()) poly.push_back({q.x, q.y});
        opt.sink->publish(nlohmann::json{{"type", "crules.keepout"}, {"name", k.zone.name}, {"layers", k.zone.layers},
                                         {"applied", cr_mode == crules::Mode::On}, {"polygon", poly}}
                              .dump());
      }
    }
    // Net classes from impedance and width rules (P5, doc 15 §16): reported in every mode, used by the router with
    // --component-rules on.
    {
      const auto ev = crules::evaluate(lb.board, &rules, cat, det, cr_mode);
      const auto sc = crules::synthetic_net_classes(lb.board, rules, cat, det, ev);
      for (const auto& c : sc) {
        std::string nets;
        for (const auto n : c.nets) nets += (nets.empty() ? "" : ", ") + lb.board.nets[static_cast<std::size_t>(n)].name;
        log("  net class " + c.cls.name + " (" + c.rule + "): " + c.detail + "; nets " + nets + (cr_mode == crules::Mode::On ? "" : " (not applied: use --component-rules on)"));
        nlohmann::json nj = nlohmann::json::array();
        for (const auto n : c.nets) nj.push_back(lb.board.nets[static_cast<std::size_t>(n)].name);
        cr_classes.push_back({{"name", c.cls.name}, {"rule", c.rule}, {"track_width_mm", nm_to_mm(c.cls.track_width)},
                              {"diff_pair_gap_mm", c.cls.has_diff_pair_gap ? nm_to_mm(c.cls.diff_pair_gap) : 0.0}, {"nets", nj},
                              {"detail", c.detail}, {"applied", cr_mode == crules::Mode::On}});
      }
      if (cr_mode == crules::Mode::On && !sc.empty()) rules = crules::with_synthetic_classes(lb.board, std::move(rules), sc);
      // --widen-power: a width a width_for_current rule computed is a target in every mode (impedance widths are
      // not: a controlled-impedance track must not be wider than computed either).
      if (job.widen_power)
        for (const auto& c : sc)
          for (const auto& cg : cat.categories)
            for (const auto& r : cg.rules)
              if (r.id == c.rule && r.kind == "width_for_current")
                for (const auto n : c.nets) widen_targets.push_back({n, c.cls.track_width, c.rule});
    }
    // --stitch-vias return: nets bound to a reference_plane rule want a via of the plane net near each of their
    // vias (doc 15 §4; the rule's stitch_via_mm, else the catalogue's general high-speed distance).
    if (stitch_return)
      for (const auto& in : det.instances) {
        if (in.confidence < cat.apply) continue;
        for (const auto& r : cat.categories[static_cast<std::size_t>(in.category)].rules) {
          if (r.kind != "reference_plane" || in.disabled(r.id)) continue;
          const std::string plane_name = r.params.contains("plane_net") && r.params["plane_net"].is_string() ? r.params["plane_net"].get<std::string>() : "GND";
          model::NetId plane = 0;
          if (const crules::Role* pr = in.role(crules::lower(plane_name)); pr && !pr->nets.empty()) plane = pr->nets.front();
          for (const auto& n : lb.board.nets)  // else the net of that name (last hierarchy component), lowest id
            if (plane == 0 && n.id > 0 && crules::upper(crules::net_leaf(n.name)) == crules::upper(plane_name)) plane = n.id;
          const std::string src = r.id + " " + lb.board.footprints[static_cast<std::size_t>(in.anchor)].reference;
          if (plane == 0) {
            log("  stitching: " + src + " names plane net " + plane_name + ", which this board does not have");
            continue;
          }
          const double mm = job.stitch_distance_mm > 0 ? job.stitch_distance_mm
                            : r.params.contains("stitch_via_mm") && r.params["stitch_via_mm"].is_number() ? r.params["stitch_via_mm"].get<double>()
                            : cat.stitch_via_within_mm;
          for (const auto& role : r.applies_to)
            if (const crules::Role* x = in.role(role))
              for (const auto n : x->nets) {
                const bool seen = std::any_of(stitch_returns.begin(), stitch_returns.end(), [&](const auto& y) { return y.signal == n && y.plane == plane; });
                if (n > 0 && n != plane && !seen) stitch_returns.push_back({n, plane, static_cast<Coord>(mm * 1e6), src});
              }
        }
      }
    // USB2-02 (P3): route each detected USB 2.0 D+/D- pair coupled first (a soft preference: the router falls back
    // to single tracks). Only pairs bound to exactly one net each.
    if (cr_mode == crules::Mode::Soft || cr_mode == crules::Mode::On)
      for (const auto& p : crules::usb_pairs(lb.board, cat, det)) {
        if (std::find(opt.pair_nets.begin(), opt.pair_nets.end(), p) != opt.pair_nets.end()) continue;
        opt.pair_nets.push_back(p);
        // The interface's own intra-pair skew limit (USB2-02 max_intra_skew_mm), unless the user gave one for all pairs.
        Coord skew = 0;
        for (const auto& c : cat.categories)
          for (const auto& r : c.rules)
            if (r.id == "USB2-02" && r.params.contains("max_intra_skew_mm") && r.params["max_intra_skew_mm"].is_number())
              skew = static_cast<Coord>(r.params["max_intra_skew_mm"].get<double>() * 1e6);
        opt.pair_net_skew.resize(opt.pair_nets.size(), 0);
        opt.pair_net_skew.back() = skew;
        log("  differential pair (USB2-02): " + lb.board.nets[static_cast<std::size_t>(p.first)].name + " / " +
            lb.board.nets[static_cast<std::size_t>(p.second)].name);
      }
    if (cr_mode == crules::Mode::On && !kos.empty()) {
      with_rules = lb.board;
      for (const auto& k : kos) {
        with_rules.zones.push_back(k.zone);
        log("  keep-out " + k.zone.name + " on " + [&] {
          std::string l;
          for (const auto& n : k.zone.layers) l += (l.empty() ? "" : "+") + n;
          return l;
        }());
      }
      route_board = &with_rules;
    }
    if (!job.out.empty()) {
      std::string side = job.out;
      if (side.ends_with(".kicad_pcb")) side.resize(side.size() - 10);
      side += ".tracemaker.kicad_dru";
      std::ofstream(side) << crules::dru_sidecar(lb.board, cat, det);
      log("component rules: generated custom rules written to " + side);
    }
  }
  // Routing intent (doc 05 §45, D110): a reviewed manifest, resolved against the board (unknown pads/nets throw).
  if (!job.intent.empty()) {
    const auto r = crules::resolve_intent(lb.board, crules::load_intent_file(job.intent), job.intent);
    opt.intent_pairs = r.pairs;
    opt.intent_plane_pads = r.plane_pads;
    opt.no_branch_nets = r.no_branch_nets;
    log(fmt("routing intent: %zu connection(s), %zu plane pad(s), %zu no-branch net(s) from %s", r.pairs.size(), r.plane_pads.size(),
            r.no_branch_nets.size(), job.intent.c_str()));
  }
  // Custom rules the router cannot apply (unreadable conditions, position-dependent disallow rules) are named
  // here, not only by `tracemaker drc`: the routed board may break them (rule 6).
  {
    const drc::RuleEngine re(*route_board, rules);
    for (const auto& w : re.warnings()) {
      log("warning: " + w);
      rule_warnings.push_back(w);
    }
  }
  // Buses (doc 05 §38): the groups asked for, with the nets found for each, so a wrong pattern shows before routing.
  std::vector<route::BusGroup> bus_groups;
  if (!job.buses.empty() || job.detect_buses) {
    std::vector<std::string> notes;
    bus_groups = resolve_buses(*route_board, rules, job.buses, job.detect_buses, job.bus_skew_mm, &notes);
    for (const auto& n : notes) log(n);
    log(fmt("buses: %zu group%s", bus_groups.size(), bus_groups.size() == 1 ? "" : "s"));
    for (const auto& g : bus_groups) {
      std::string nets;
      for (const auto n : g.members) nets += (nets.empty() ? "" : " ") + lb.board.nets[static_cast<std::size_t>(n)].name;
      log(fmt("  bus %s (%s, %zu nets, %s): %s", g.name.c_str(), g.source.c_str(), g.members.size(),
              g.skew_limit > 0 ? fmt("skew limit %.3f mm from the %s", nm_to_mm(g.skew_limit), g.skew_source.c_str()).c_str() : "no skew limit", nets.c_str()));
      opt.buses.push_back({g.name, g.members, g.skew_limit});
    }
  }
  auto& res = out.result;
  const auto route_t0 = std::chrono::steady_clock::now();  // --time covers the whole job, the refill repair included
  std::vector<int> ran;
  int best_index = 0;
  std::string best_name;
  nlohmann::json variant_rows = nlohmann::json::array();
  // The variant set is a setting, never derived from the thread count when a work budget makes the run
  // deterministic: then `--threads` only changes how fast the same variants finish (requirement N3, D47).
  const int threads = std::max(1, job.threads);
  // An explicit --variants may go beyond the eight designed variants: the extras are seeded diversifications
  // (doc 16: on hard dense breakouts the winning variant is spread over the portfolio, so more seeds find more).
  const int variants = std::clamp(job.variants > 0 ? job.variants : opt.work_budget > 0 ? route::portfolio_size() : threads, 1,
                                  job.variants > 0 ? 64 : route::portfolio_size());
  route::WorkProfile all_work;  // all portfolio variants and refill repair, including discarded attempts
  if (variants > 1) {
    std::vector<int> pick;
    log(fmt("portfolio: %d variants on %d thread%s", variants, std::min(threads, variants), std::min(threads, variants) == 1 ? "" : "s"));
    auto pr = route::route_portfolio(*route_board, rules, opt, variants, pick, threads);
    for (std::size_t i = 0; i < pr.variants.size(); ++i) {
      log(fmt("  variant %d %-30s routed %d in %.1f s%s", pr.indices[i], pr.variants[i].c_str(), pr.routed[i], pr.seconds[i],
              static_cast<int>(i) == pr.best_variant ? "  <- best" : ""));
      variant_rows.push_back({{"variant", pr.indices[i]}, {"name", pr.variants[i]}, {"routed", pr.routed[i]}, {"seconds", pr.seconds[i]}});
    }
    ran = pr.indices;
    best_index = pr.indices[static_cast<std::size_t>(pr.best_variant)];
    best_name = pr.variants[static_cast<std::size_t>(pr.best_variant)];
    res = std::move(pr.best);
    for (const auto& w : pr.work)
      for (int p = 0; p < route::kWorkPhases; ++p) all_work.phase[static_cast<std::size_t>(p)] += w.phase[static_cast<std::size_t>(p)];
  } else {
    res = route::Router(*route_board, rules, opt).run();
    ran = {0};
    all_work = res.work;
  }
  if (kb) {
    kb->record_run(feat, name, ran, best_index, res.routed, res.connections, res.seconds);
    std::vector<learn::FailedConnection> failed;
    for (const auto& u : res.unrouted) failed.push_back({u.net, u.a, u.b});
    kb->record_failures(feat.hash, failed);
  }
  // Soft zones (doc 05 §36): the router cut through stale fills and trusted them for connectivity. Refill them for
  // the new copper as KiCad will, count what is still unconnected, and route those connections again with the
  // refilled fills as fixed copper (pass 2, its own budget equal to the first pass's).
  nlohmann::json refill_summary;
  int unconnected_after_refill = -1;
  if (opt.soft_zones) {
    model::Board routed = *route_board;
    auto add_copper = [](model::Board& b, const route::RouteResult& r) {
      b.tracks.insert(b.tracks.end(), r.tracks.begin(), r.tracks.end());
      b.vias.insert(b.vias.end(), r.vias.begin(), r.vias.end());
    };
    auto unconnected = [&](const model::Board& b) {
      drc::DrcOptions d;
      d.connectivity_only = true;
      d.dangling = false;
      return static_cast<int>(drc::run_drc(b, rules, d).unconnected.size());
    };
    add_copper(routed, res);
    auto rf = drc::refill_zones(routed, rules);
    for (const auto& w : rf.warnings) log("warning: " + w);
    const int before = unconnected(rf.board);
    log(fmt("soft zones: after refilling %d zones, %d items unconnected", rf.zones, before));
    refill_summary = {{"zones", rf.zones}, {"unconnected", before}, {"warnings", rf.warnings}};
    unconnected_after_refill = before;
    // The repair has the first pass's work budget but only what is left of the time limit (rule 5).
    const double time_left = opt.time_limit_s - std::chrono::duration<double>(std::chrono::steady_clock::now() - route_t0).count();
    if (before > 0 && job.refill_repair && time_left <= 0) log("refill repair: skipped, the time limit is used up");
    if (before > 0 && job.refill_repair && time_left > 0) {
      route::RouterOptions o2 = opt;
      o2.soft_zones = false;
      // Existing routed copper is fixed in this connectivity repair. Re-probing coupled
      // routes cannot negotiate it away and only starves the missing single connections.
      o2.diff_pairs = false;
      o2.pair_nets.clear();
      o2.sink = nullptr;
      o2.time_limit_s = time_left;
      auto r2 = route::Router(rf.board, rules, o2).run();
      for (int p = 0; p < route::kWorkPhases; ++p)
        all_work.phase[static_cast<std::size_t>(p)] += r2.work.phase[static_cast<std::size_t>(p)];
      log(fmt("refill repair: routed %d/%d connections, %zu tracks, %zu vias, %ld expansions", r2.routed, r2.connections, r2.tracks.size(),
              r2.vias.size(), r2.expansions));
      // The repair's copper changes the fills once more. It is kept only if the count after that refill is lower
      // (rule 4); otherwise the first pass stands.
      add_copper(routed, r2);
      const int after = unconnected(drc::refill_zones(routed, rules).board);
      const bool kept = after < before;
      log(fmt("refill repair: %d items unconnected after refilling again%s", after, kept ? "" : " (no better: repair discarded)"));
      res.pair_successes += r2.pair_successes;
      res.pair_work += r2.pair_work;
      res.pair_anchor_skips += r2.pair_anchor_skips;
      res.pair_attempts.reserve(res.pair_attempts.size() + r2.pair_attempts.size());
      for (auto& attempt : r2.pair_attempts) {
        attempt.refill_repair = true;
        res.pair_attempts.push_back(std::move(attempt));
      }
      if (kept) {
        res.tracks.insert(res.tracks.end(), r2.tracks.begin(), r2.tracks.end());
        res.vias.insert(res.vias.end(), r2.vias.begin(), r2.vias.end());
        res.pairs += r2.pairs;
        unconnected_after_refill = after;
      }
      refill_summary["repair"] = {{"connections", r2.connections}, {"routed", r2.routed}, {"tracks", r2.tracks.size()},
                                  {"vias", r2.vias.size()}, {"expansions", r2.expansions}, {"unconnected", after}, {"kept", kept}};
      refill_summary["repair"]["work"] = work_json(r2.work);
      refill_summary["repair"]["stop"] = r2.stop;
    }
    out.unconnected_after_refill = unconnected_after_refill;
  }
  // After-route passes (doc 05 §39). Each works on the routed copper as a transaction: its result is judged by
  // the number of unconnected items (after a refill when fills are soft or vias were added) and dropped if that
  // number went up.
  nlohmann::json widen_summary, stitch_summary;
  {
    // Unconnected items per net (keyed by the report's text, which names the net), and all of them under "".
    using Unconnected = std::map<std::string, int>;
    auto judge = [&](const std::vector<model::Track>& tracks, const std::vector<model::Via>& vias, bool refill) {
      model::Board bd = *route_board;
      bd.tracks.insert(bd.tracks.end(), tracks.begin(), tracks.end());
      bd.vias.insert(bd.vias.end(), vias.begin(), vias.end());
      drc::DrcOptions d;
      d.connectivity_only = true;
      d.dangling = false;
      Unconnected n;
      n[""] = 0;
      for (const auto& v : drc::run_drc(refill ? drc::refill_zones(bd, rules).board : bd, rules, d).unconnected) ++n[v.description], ++n[""];
      return n;
    };
    // No net may have more unconnected items with a pass's result than without it. The total alone would let a
    // connection the pass made in one net (a stitching via that ties a loose piece of its plane) hide one it broke
    // in another.
    auto no_worse = [](const Unconnected& without, const Unconnected& with) {
      for (const auto& [net, n] : with) {
        const auto it = without.find(net);
        if (n > (it == without.end() ? 0 : it->second)) return false;
      }
      return true;
    };
    auto mm = [](Coord v) { return fmt("%.3f", nm_to_mm(v)); };
    if (job.widen_power) {
      if (opt.sink) opt.sink->publish(server::stage("widen", "begin", "power track widening"));
      passes::WidenOptions wo;
      wo.targets = widen_targets;
      wo.soft_zones = opt.soft_zones;
      wo.work_limit = job.pass_work;
      // Coupled pairs keep their pair width.
      {
        const drc::RuleEngine re(lb.board, rules);
        auto pairs = opt.diff_pairs ? route::named_pairs(lb.board, re) : std::vector<std::pair<model::NetId, model::NetId>>{};
        pairs.insert(pairs.end(), opt.pair_nets.begin(), opt.pair_nets.end());
        for (const auto& [a, b2] : pairs) wo.skip_nets.push_back(a), wo.skip_nets.push_back(b2);
      }
      auto widened = res.tracks;
      const auto wr = passes::widen_tracks(*route_board, rules, widened, res.vias, wo);
      bool kept = wr.widened > 0;
      int before = -1, after = -1;
      if (kept) {
        const auto without = judge(res.tracks, res.vias, opt.soft_zones), with = judge(widened, res.vias, opt.soft_zones);
        before = without.at("");
        after = with.at("");
        kept = no_worse(without, with);
      }
      nlohmann::json nets = nlohmann::json::array();
      Coord total = 0;
      for (const auto& n : wr.nets) {
        auto widths = [&](const std::map<Coord, Coord>& m) {
          nlohmann::json j = nlohmann::json::object();
          for (const auto& [w, len] : m) j[mm(w)] = nm_to_mm(len);
          return j;
        };
        auto text = [&](const std::map<Coord, Coord>& m) {
          std::string t;
          for (const auto& [w, len] : m) t += (t.empty() ? "" : ", ") + fmt("%.2f mm at %s", nm_to_mm(len), mm(w).c_str());
          return t;
        };
        const std::string& net_name = lb.board.nets[static_cast<std::size_t>(n.net)].name;
        total += n.widened_length;
        nets.push_back({{"net", net_name}, {"target_mm", nm_to_mm(n.target)}, {"source", n.source}, {"tracks_below_target", n.tracks},
                        {"tracks_widened", n.widened}, {"tracks_at_target", n.at_target}, {"widened_mm", nm_to_mm(n.widened_length)},
                        {"before_mm_by_width", widths(n.before)}, {"after_mm_by_width", widths(kept ? n.after : n.before)}});
        if (n.widened > 0)
          log(fmt("  widen %s (target %s mm, %s): %d of %d tracks, %.2f mm; before %s; after %s", net_name.c_str(), mm(n.target).c_str(), n.source.c_str(),
                  n.widened, n.tracks, nm_to_mm(n.widened_length), text(n.before).c_str(), text(n.after).c_str()));
      }
      log(fmt("widen power: %d tracks of %zu nets widened, %.2f mm, %ld checks%s%s", wr.widened, wr.nets.size(), nm_to_mm(total), wr.checks,
              wr.out_of_work ? " (work budget used up)" : "",
              wr.widened > 0 && !kept ? fmt("; discarded: a net has more unconnected items with it (%d in all, %d without)", after, before).c_str() : ""));
      widen_summary = {{"tracks_widened", kept ? wr.widened : 0}, {"widened_mm", kept ? nm_to_mm(total) : 0.0}, {"checks", wr.checks},
                       {"out_of_work", wr.out_of_work}, {"kept", kept}, {"nets", nets}};
      if (kept) {
        res.tracks = std::move(widened);
        if (unconnected_after_refill >= 0) out.unconnected_after_refill = unconnected_after_refill = after;
      }
      if (opt.sink) opt.sink->publish(server::stage("widen", "end", fmt("%d tracks widened", kept ? wr.widened : 0)));
    }
    if (stitch_planes || stitch_return) {
      if (opt.sink) opt.sink->publish(server::stage("stitch", "begin", "stitching vias"));
      passes::StitchOptions so;
      so.planes = stitch_planes;
      so.pitch = static_cast<Coord>((job.stitch_pitch_mm > 0 ? job.stitch_pitch_mm : crules::builtin_catalogue().stitch_via_within_mm) * 1e6);
      so.returns = stitch_returns;
      so.work_limit = job.pass_work;
      model::Board bd = *route_board;
      bd.tracks.insert(bd.tracks.end(), res.tracks.begin(), res.tracks.end());
      bd.vias.insert(bd.vias.end(), res.vias.begin(), res.vias.end());
      const auto sr = passes::stitch_vias(bd, rules, so);
      for (const auto& w : sr.warnings) log("warning: " + w);
      bool kept = !sr.vias.empty();
      int before = -1, after = -1;
      if (kept) {
        auto with = res.vias;
        with.insert(with.end(), sr.vias.begin(), sr.vias.end());
        const auto without = judge(res.tracks, res.vias, true), with_vias = judge(res.tracks, with, true);
        before = without.at("");
        after = with_vias.at("");
        kept = no_worse(without, with_vias);
      }
      std::map<model::NetId, std::pair<int, int>> per_net;  // net -> (grid vias, return-path vias)
      for (std::size_t i = 0; i < sr.vias.size() && kept; ++i) ++(sr.return_path[i] ? per_net[sr.vias[i].net].second : per_net[sr.vias[i].net].first);
      nlohmann::json nets = nlohmann::json::array(), rets = nlohmann::json::array();
      int grid = 0, ret = 0;
      for (const auto& [n, c] : per_net) {
        const std::string& net_name = lb.board.nets[static_cast<std::size_t>(n)].name;
        nets.push_back({{"net", net_name}, {"plane", c.first}, {"return_path", c.second}});
        log(fmt("  stitch %s: %d plane vias, %d return-path vias", net_name.c_str(), c.first, c.second));
        grid += c.first;
        ret += c.second;
      }
      for (const auto& r : sr.returns) {
        const std::string& sig = lb.board.nets[static_cast<std::size_t>(r.signal)].name;
        const std::string& pl = lb.board.nets[static_cast<std::size_t>(r.plane)].name;
        const int now = kept ? r.stitched_after : r.stitched_before;
        rets.push_back({{"net", sig}, {"plane_net", pl}, {"source", r.source}, {"distance_mm", nm_to_mm(r.distance)}, {"signal_vias", r.signal_vias},
                        {"stitched_before", r.stitched_before}, {"stitched_after", now}, {"unstitched_before", r.signal_vias - r.stitched_before},
                        {"unstitched_after", r.signal_vias - now}});
        log(fmt("  return path %s over %s (%s, within %s mm): %d vias, %d stitched before, %d after", sig.c_str(), pl.c_str(), r.source.c_str(),
                mm(r.distance).c_str(), r.signal_vias, r.stitched_before, now));
      }
      if (stitch_return && sr.returns.empty()) log("  stitching: no net needs a reference plane (component rules of kind reference_plane, or --stitch-return)");
      log(fmt("stitch vias: %d plane, %d return-path; %d grid sites in overlapping fills (%d already tied, %d blocked), %d dropped after the refill, "
              "%ld checks%s%s",
              grid, ret, sr.sites, sr.site_served, sr.site_blocked, sr.dropped, sr.checks, sr.out_of_work ? " (work budget used up)" : "",
              sr.discarded ? "; withdrawn: the refill with the vias cut a fill"
              : !sr.vias.empty() && !kept ? fmt("; discarded: a net has more unconnected items with them (%d in all, %d without)", after, before).c_str() : ""));
      stitch_summary = {{"plane_vias", grid}, {"return_path_vias", ret}, {"sites", sr.sites}, {"sites_already_tied", sr.site_served},
                        {"sites_blocked", sr.site_blocked}, {"dropped", sr.dropped}, {"checks", sr.checks}, {"out_of_work", sr.out_of_work},
                        {"kept", kept}, {"pitch_mm", nm_to_mm(so.pitch)}, {"nets", nets}, {"return_paths", rets}};
      if (kept) {
        for (std::size_t i = 0; i < sr.vias.size(); ++i) {
          // Ids above the router's own, so the viewer shows the vias without replacing anything.
          if (opt.sink) opt.sink->publish(server::via_add(static_cast<server::ObjectId>(1'000'000'000 + i), sr.vias[i]));
          res.vias.push_back(sr.vias[i]);
        }
        if (unconnected_after_refill >= 0) out.unconnected_after_refill = unconnected_after_refill = after;
      }
      if (opt.sink) opt.sink->publish(server::stage("stitch", "end", fmt("%d vias added", grid + ret)));
    }
  }
  if (!job.out.empty()) {
    io::BoardEditor ed(lb, opt.seed);
    for (const auto& t : res.tracks) ed.add_track(t);
    for (const auto& v : res.vias) ed.add_via(v);
    ed.save(job.out);
  }
  // The whole job includes every portfolio variant, refill repair and after-route passes (D86).
  const double job_seconds = std::chrono::duration<double>(std::chrono::steady_clock::now() - t_job).count();
  log(fmt("routed %d/%d planned connections, %zu tracks, %zu vias, pitch %.3f mm, %ld expansions, %.2f s (best variant %.2f s), stopped: %s",
          res.routed, res.connections, res.tracks.size(), res.vias.size(), nm_to_mm(res.pitch), res.expansions, job_seconds, res.seconds,
          res.stop.empty() ? "complete" : res.stop.c_str()));
  if (res.stop == "time")
    log(fmt("warning: the run hit its time limit (--time %.0f s) before its work budget, so the result depends on machine speed and load; "
            "raise --time or lower --work for repeatable output", job.opt.time_limit_s));
  for (const auto& f : res.failures) log("  unrouted: " + f);
  {
    const auto t = all_work.total();
    log(fmt("work (all variants and refill repair): %ld expansions, %ld flood cells, %ld cell checks, %ld fixed-copper checks, %ld field cells in %ld fields, "
            "%ld commits, %ld rips",
            t.expansions, t.flood_cells, t.cell_checks, t.fixed_checks, t.field_cells, t.fields, t.commits, t.rips));
  }
  for (std::size_t r = 0; r < res.escape_rings.size(); ++r)
    log(fmt("  deep-array ring %zu: %d of %d pins connected", r + 1, res.escape_rings[r].second, res.escape_rings[r].first));
  if (opt.cut_report) {
    auto line = [&](const route::CutLine& c) {
      return fmt("%s = %.2f mm: %d nets must cross, %d tracks fit", c.vertical ? "x" : "y", nm_to_mm(c.at), c.demand, c.capacity);
    };
    if (res.tightest_cut.demand > 0) log("  tightest cut line " + line(res.tightest_cut));
    for (const auto& c : res.over_cuts) log("  cut proof: unroutable across " + line(c));
  }
  out.items = items_json(lb.board, res);
  if (!job.items_out.empty()) std::ofstream(job.items_out) << out.items.dump();
  out.summary = {{"routed", res.routed},     {"connections", res.connections}, {"tracks", res.tracks.size()},
                 {"vias", res.vias.size()},  {"seconds", job_seconds},         {"variant_seconds", res.seconds},
                 {"expansions", res.expansions}, {"pitch_mm", nm_to_mm(res.pitch)}, {"failures", res.failures},
                 {"variant", best_index}, {"variant_name", best_name}, {"escape_corridors", res.escape_corridors},
                 {"stop", res.stop.empty() ? "complete" : res.stop}};
  out.summary["rules"] = {{"project", sources.project}, {"dru", sources.dru}, {"warnings", rule_warnings},
                          {"project_loaded", std::filesystem::exists(sources.project)}, {"dru_loaded", std::filesystem::exists(sources.dru)}};
  if (job.protect_planes) {
    out.summary["protected_planes"] = nlohmann::json::array();
    for (const auto& plane : protected_planes)
      out.summary["protected_planes"].push_back({{"layer", lb.board.copper_name(plane.layer)},
          {"net", lb.board.nets[static_cast<std::size_t>(plane.net)].name}, {"coverage", plane.coverage}});
  }
  if (!job.intent.empty()) {
    out.summary["intent"] = {{"file", job.intent}, {"connections", opt.intent_pairs.size()}, {"plane_pads", opt.intent_plane_pads.size()},
                             {"no_branch_nets", opt.no_branch_nets.size()}, {"relaxed", res.intent_relaxed}};
    log(fmt("routing intent: %zu intent connection(s) relaxed to ordinary routing", res.intent_relaxed.size()));
    for (const auto& r : res.intent_relaxed) log("  relaxed: " + r);
  }
  out.summary["unrouted"] = nlohmann::json::array();
  auto layer_names = [&](model::LayerMask mask) {
    std::vector<std::string> names;
    for (int l = 0; l < lb.board.copper_count(); ++l)
      if (mask & model::layer_bit(l)) names.push_back(lb.board.copper_name(l));
    return names;
  };
  for (const auto& u : res.unrouted)
    out.summary["unrouted"].push_back({{"net", u.net}, {"a", u.a}, {"b", u.b}, {"reason", u.reason}, {"detail", u.detail},
                                     {"a_mm", {nm_to_mm(u.a_pos.x), nm_to_mm(u.a_pos.y)}},
                                     {"b_mm", u.has_b_position ? nlohmann::json{nm_to_mm(u.b_pos.x), nm_to_mm(u.b_pos.y)} : nlohmann::json(nullptr)},
                                     {"a_layers", layer_names(u.a_layers)}, {"b_layers", layer_names(u.b_layers)}});
  if (!variant_rows.empty()) out.summary["variants"] = std::move(variant_rows);
  if (!refill_summary.is_null()) {
    out.summary["refill"] = refill_summary;
    out.summary["unconnected_after_refill"] = unconnected_after_refill;
  }
  if (!cr_classes.empty()) out.summary["component_rule_net_classes"] = cr_classes;
  if (!widen_summary.is_null()) out.summary["widen"] = widen_summary;
  if (!stitch_summary.is_null()) out.summary["stitch"] = stitch_summary;
  // Style metric of the new copper (doc 05 §39.2): measured only, never fed back into routing.
  out.summary["style"] = passes::style_json(lb.board, passes::measure_style(lb.board, res.tracks, {}, res.vias));
  out.summary["work"] = work_json(res.work);
  out.summary["work_all_variants"] = work_json(all_work);
  // Differential pairs (doc 05 §15): how each wanted pair came out, measured on the new copper (only when pairs are on).
  if (opt.diff_pairs || !opt.pair_nets.empty()) {
    const drc::RuleEngine re(lb.board, rules);
    auto want = opt.diff_pairs ? route::named_pairs(lb.board, re) : std::vector<std::pair<model::NetId, model::NetId>>{};
    for (const auto& p : opt.pair_nets)
      if (std::find(want.begin(), want.end(), p) == want.end() && std::find(want.begin(), want.end(), std::make_pair(p.second, p.first)) == want.end())
        want.push_back(p);
    nlohmann::json pj = nlohmann::json::array();
    int fallback_attempts = 0;
    long attempt_work = 0;
    std::map<std::string, int> failure_classes;
    std::set<std::pair<model::NetId, model::NetId>> attempted;
    nlohmann::json attempts = nlohmann::json::array();
    for (const auto& attempt : res.pair_attempts) {
      attempt_work += attempt.work;
      attempted.emplace(std::min(attempt.a, attempt.b), std::max(attempt.a, attempt.b));
      if (!attempt.coupled) { ++fallback_attempts; ++failure_classes[attempt.reason]; }
      nlohmann::json pads = nlohmann::json::array();
      for (const auto p : attempt.pads) pads.push_back({{"x_mm", nm_to_mm(p.x)}, {"y_mm", nm_to_mm(p.y)}});
      attempts.push_back({{"net_a", lb.board.nets[static_cast<std::size_t>(attempt.a)].name},
                         {"net_b", lb.board.nets[static_cast<std::size_t>(attempt.b)].name}, {"pads", pads},
                         {"phase", route::work_phase_name(attempt.phase)}, {"cleanup", attempt.cleanup}, {"refill_repair", attempt.refill_repair},
                         {"coupled", attempt.coupled}, {"reason", attempt.reason}, {"detail", attempt.detail}, {"work", attempt.work}});
    }
    out.summary["pair_routing"] = {{"scope", "selected_variant_and_refill_repair"}, {"wanted_net_pairs", want.size()}, {"attempted_net_pairs", attempted.size()},
        {"attempts", res.pair_attempts.size()}, {"successful_attempts", res.pair_successes}, {"fallback_attempts", fallback_attempts},
        {"surviving_routes", res.pairs}, {"anchored_cleanup_skips", res.pair_anchor_skips}, {"failure_classes", failure_classes}, {"details", attempts}};
    log(fmt("differential pairs: %zu wanted, %zu attempts, %d successful, %d fallback, %d surviving coupled routes, %ld work units",
            want.size(), res.pair_attempts.size(), res.pair_successes, fallback_attempts, res.pairs, res.pair_work));
    out.summary["pair_work"] = res.pair_work;
    out.summary["pair_setup_work"] = res.pair_work - attempt_work;
    for (const auto& [na, nb] : want) {
      const auto pr = route::pair_rule(lb.board, rules, re, na, nb);
      const auto st = route::measure_pair(res.tracks, res.vias, na, nb, route::coupled_threshold(pr));
      const std::string& a = lb.board.nets[static_cast<std::size_t>(na)].name;
      const std::string& b = lb.board.nets[static_cast<std::size_t>(nb)].name;
      log(fmt("  %s / %s: %.1f mm, coupled %.0f %%, gap %.3f mm (target %.3f, %s), skew %.3f mm, vias %d/%d", a.c_str(), b.c_str(),
              (st.length_a + st.length_b) / 2e6, 100 * st.coupled_share(), st.gap_median / 1e6, nm_to_mm(pr.gap), pr.source.c_str(), st.skew() / 1e6,
              st.vias_a, st.vias_b));
      pj.push_back({{"net_a", a}, {"net_b", b}, {"coupled_share", st.coupled_share()}, {"gap_mm", st.gap_median / 1e6}, {"target_gap_mm", nm_to_mm(pr.gap)},
                    {"skew_mm", st.skew() / 1e6}, {"length_mm", (st.length_a + st.length_b) / 2e6},
                    {"vias_a", st.vias_a}, {"vias_b", st.vias_b}, {"via_imbalance", std::abs(st.vias_a - st.vias_b)}});
    }
    out.summary["pairs"] = pj;
  }
  // Buses: every group measured on the board as written (existing copper and the new), not on the router's own
  // book-keeping; `tracemaker buses` gives the same numbers for the output file.
  if (!bus_groups.empty()) {
    model::Board routed = lb.board;
    routed.tracks.insert(routed.tracks.end(), res.tracks.begin(), res.tracks.end());
    routed.vias.insert(routed.vias.end(), res.vias.begin(), res.vias.end());
    const auto joined = route::nets_joined(routed, rules);
    nlohmann::json bj = nlohmann::json::array();
    log(fmt("buses: %d connections routed as lanes, %d left their bus", res.bus_lanes, res.bus_left));
    for (const auto& g : bus_groups) {
      std::string line;
      bj.push_back(bus_report(routed, rules, joined, g, &line));
      log("  " + line);
    }
    out.summary["buses"] = bj;
    out.summary["bus_lanes"] = res.bus_lanes;
    out.summary["bus_left"] = res.bus_left;
  }
  if (opt.cut_report) {
    auto cj = [](const route::CutLine& c) {
      return nlohmann::json{{"axis", c.vertical ? "x" : "y"}, {"at_mm", nm_to_mm(c.at)}, {"demand", c.demand}, {"capacity", c.capacity}};
    };
    nlohmann::json over = nlohmann::json::array();
    for (const auto& c : res.over_cuts) over.push_back(cj(c));
    out.summary["cuts"] = {{"tightest", cj(res.tightest_cut)}, {"unroutable", over}};
  }
  if (!res.escape_rings.empty()) {
    nlohmann::json rings = nlohmann::json::array();
    for (const auto& [pins, done] : res.escape_rings) rings.push_back({{"pins", pins}, {"connected", done}});
    out.summary["escape_rings"] = rings;
  }
  if (!job.json_out.empty()) std::ofstream(job.json_out) << out.summary.dump(1);
  if (server && job.hold) {
    log("routing finished; viewer still serving at " + server->url() + " (Ctrl-C to quit)");
    for (;;) std::this_thread::sleep_for(std::chrono::seconds(1));
  }
  opt.sink = nullptr;
  return out;
}

}  // namespace tmk::app
