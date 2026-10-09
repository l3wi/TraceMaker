// SPDX-License-Identifier: GPL-3.0-or-later
#include "app/route_job.hpp"
#include <CLI/CLI.hpp>

#include <algorithm>
#include <chrono>
#include <cstdarg>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <memory>
#include <mutex>
#include <sstream>
#include <thread>

#include "crules/engine.hpp"
#include "drc/copper.hpp"
#include "drc/rule_engine.hpp"
#include "route/diff_pair.hpp"
#include "route/layer_limits.hpp"
#include "gpu/device.hpp"
#include "io/kicad/board_editor.hpp"
#include "io/kicad/board_reader.hpp"
#include "io/kicad/project_reader.hpp"
#include "learn/knowledge_base.hpp"
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

}  // namespace

int default_gpu_device(bool use_gpu) {
  if (!use_gpu) return -1;
  const auto devs = gpu::list_devices();
  return devs.empty() ? -1 : devs.front().index;
}

RouteJob::RouteJob() : kb_path(learn::KnowledgeBase::default_path()) {}

void add_route_options(CLI::App& app, RouteJob& job, bool require_paths) {
  auto* route = &app;
  struct ParseState {
    double pitch_um, pair_skew_mm;
    bool no_kb = false;
  };
  const auto state = std::make_shared<ParseState>(ParseState{job.opt.pitch / 1000.0, job.opt.pair_skew / 1e6});
  auto& ropt = job.opt;
  auto& r_job = job;
  auto& r_pitch_um = state->pitch_um;
  auto& r_pair_skew_mm = state->pair_skew_mm;
  auto& r_nokb = state->no_kb;
  auto& r_threads = job.threads;
  auto& r_variants = job.variants;
  auto& r_kb = job.kb_path;
  auto& r_json = job.json_out;
  auto& r_items = job.items_out;
  auto* board = route->add_option("board", job.in)->check(CLI::ExistingFile);
  auto* output = route->add_option("-o,--output", job.out, "Output .kicad_pcb");
  if (require_paths) board->required(), output->required();
  route->add_option("--project", job.project, "Read this project and its same-stem .kicad_dru instead of the board's stem");
  route->add_option("--time", ropt.time_limit_s, "Time limit, seconds (safety net; results then depend on machine speed)");
  route->add_option("--work", ropt.work_budget, "Deterministic work budget in search expansions per router (e.g. 50000000)");
  route->add_option("--pitch-um", r_pitch_um, "Lattice pitch in micrometres (default: automatic)");
  route->add_option("--via-cost-mm", ropt.via_cost_mm, "Cost of a via as equivalent track length");
  route->add_option("--seed", ropt.seed);
  route->add_option("--only-net", ropt.only_net, "Debugging: route only this net")->group("");
  route->add_option("--skip-net", ropt.skip_nets, "Experiments: leave this net unrouted (repeatable)")->group("");
  route->add_option("--soft-attempts", ropt.soft_attempts, "Window sizes tried by negotiated searches (1-4)")->group("");
  route->add_option("--heuristic-weight", ropt.heuristic_weight, "Weighted A* factor (1.0 = optimal searches)");
  route->add_flag("!--no-rip-up", ropt.rip_up, "Disable negotiated rip-up and reroute");
  route->add_flag("--micro-vias", ropt.micro_vias, "Use micro vias (outer layer to the next) where a through via is blocked (only on boards that allow them)");
  route->add_flag("--blind-vias", ropt.blind_vias, "Use blind/buried vias where a through via is blocked (only on boards that allow them)");
  route->add_flag("--diff-pairs", ropt.diff_pairs, "Route differential pairs (KiCad P/N or +/- names) as coupled pairs first (doc 05 §15)");
  route->add_flag("--pair-twists,!--no-pair-twists", ropt.pair_twists, "Coupled pairs may swap sides at a layer change, for pairs whose pin order is mirrored between the ends (default on; only with pair routing)");
  route->add_option("--pair-skew-mm", r_pair_skew_mm, "Intra-pair skew limit for coupled pairs: meanders on the shorter half (0 = custom skew rules only)");
  route->add_flag("--global", ropt.global_route, "Global routing first: detailed search follows coarse corridors");
  route->add_flag("--global-confine", ropt.global_confine, "With --global: confine each connection's first search to its corridor (experimental)")->group("");
  route->add_flag("--global-corridor-only", ropt.global_strict_corridor_only, "With --global-confine: a corridor failure goes straight to negotiation (experimental)")->group("");
  route->add_option("--max-expansions", ropt.max_expansions, "Search expansions per attempt")->group("");
  route->add_flag("!--no-optimize", ropt.optimize, "Skip the post-routing clean-up pass (fewer vias, shorter tracks)");
  route->add_flag("--plan-gap,!--no-plan-gap", ropt.plan_gap, "Plan connections by copper-to-copper distance between pads rather than centre to centre (default on)");
  route->add_flag("--tree-attach,!--no-tree-attach", ropt.tree_attach,
                  "Connections may start or end on any copper already joined to their pads (branch off the net's tree)");
  route->add_flag("--plane-nets-last,!--no-plane-nets-last", ropt.plane_nets_last,
                  "Nets with a plane connection route their pad-to-pad connections after all other connections");
  route->add_flag("--global-congestion", ropt.global_congestion, "Global plan as a congestion map: through traffic pays in tiles the plan fills (experimental)")->group("");
  route->add_option("--global-congestion-from", ropt.global_congestion_from, "Congestion threshold, eighths of tile capacity")->group("");
  route->add_option("--global-congestion-pen", ropt.global_congestion_pen, "Congestion cost, pitches per step per eighth above the threshold")->group("");
  route->add_flag("--escape-plan,!--no-escape-plan", ropt.escape_plan, "Reserve escape corridors for the pins of dense packages (QFP, BGA) before routing");
  route->add_flag("--escape-second-ring", ropt.escape_second_ring, "Escape plan: second-ring balls between two outer balls (else dog-bone vias)")->group("");
  route->add_flag("--escape-flow", ropt.escape_flow,
                  "Escape plan v2: min-cost-flow channel and layer assignment for deep BGA arrays (implies --escape-plan; experimental)");
  route->add_flag("--reroute", job.reroute, "Remove every unlocked track and via first and route the whole board again");
  route->add_option("--reroute-nets", job.reroute_nets, "Remove unlocked tracks, arcs and vias of these nets only before routing")->delimiter(',');
  route->add_flag("--fine-variant,!--no-fine-variant", ropt.fine_variant, "Portfolio: one variant routes small boards at half the lattice pitch (default on)")->group("");
  route->add_flag("--halving", ropt.halving, "With more --variants than --threads: successive halving over all the variants instead of running only the first ones");
  route->add_flag("--cut-report", ropt.cut_report, "Report straight lines across the board that more nets must cross than tracks fit (proof that the placement cannot be routed)");
  route->add_flag("--escape-report", ropt.escape_report, "Report the pins of deep BGA arrays per ring and how many were connected");
  route->add_flag("!--fast-bends", ropt.bend_states, "Approximate bend costs (1 state per lattice point instead of 9)");
  route->add_flag("!--no-gpu", job.use_gpu, "Compute cost-to-go fields on the CPU instead of the GPU (same results)");
  route->add_flag("--reach-verify", ropt.reach_verify, "Test: check every unreachable verdict with the full A*")->group("");
  route->add_option("--reach-check", ropt.reach_check, "Reachability check before strict searches: 0 off, 1 likely failures, 2 all")->group("");
  route->add_flag("--soft-zones", ropt.soft_zones, "Zone fills do not block other nets (refill the zones afterwards); unused fills become plane targets");
  route->add_flag("--via-in-pad", ropt.via_in_pad, "Inner balls / enclosed SMD pads may take a minimum-size via in the pad (needs filled, capped vias)");
  route->add_flag("--keep-vias-off-pads", ropt.vias_off_pads, "Vias keep clear of SMD pads narrower than --vias-off-pads-below (via-in-pad excepted)");
  route->add_option("--vias-off-pads-below", ropt.vias_off_pads_below_mm, "Pad width (mm) below which --keep-vias-off-pads applies (default 2)");
  route->add_option("--first-nets", ropt.first_nets, "Comma-separated nets routed first, also after restarts; other nets do not rip them (e.g. crystal lines)")->delimiter(',');
  route->add_flag("!--no-field", ropt.field_heuristic, "Use the octile heuristic only (no cost-to-go fields)");
  route->add_option("--kb", r_kb, "Knowledge base file (failure memory across runs)");
  route->add_flag("--no-kb", r_nokb, "Do not read or update the knowledge base");
  route->add_option("--threads", r_threads,
                    "Threads for the portfolio (differently configured routers, best kept). Without --variants it is also the portfolio "
                    "size, except with --work, where all variants run and the output is identical for any thread count");
  route->add_option("--variants", r_variants,
                    "Portfolio size (1 = single router; default: all with --work, else --threads); the output never depends on --threads "
                    "with --work. --time applies to each variant from its start");
  route->add_option("--no-tracks-on", r_job.no_tracks_on, "Copper layers that get no new tracks, e.g. In1.Cu,In2.Cu (vias still pass through and reach zone fills there)")
      ->delimiter(',');
  route->add_option("--layer-cost", r_job.layer_costs, "Track cost per layer as LAYER=FACTOR, e.g. In1.Cu=4,In2.Cu=4 (1 = normal, at most 1000)")->delimiter(',');
  route->add_flag("--view", r_job.view, "Stream the routing live to the browser viewer");
  route->add_option("--record", r_job.record, "Write the routing events (JSON lines, time-stamped) to a file for replay; zstd-compressed if FILE ends in .zst");
  route->add_option("--view-host", r_job.view_host, "Viewer bind address (default 0.0.0.0)");
  route->add_option("--view-port", r_job.view_port, "Viewer port (default 8766)");
  route->add_flag("--hold", r_job.hold, "Keep serving the viewer after routing finishes");
  route->add_option("--json", r_json, "Write a result summary as JSON");
  route->add_option("--component-rules", r_job.component_rules,
                    "Component-aware rules (doc 15): off (default); report/soft write <output>.tracemaker.kicad_dru; on also routes "
                    "with the generated keep-outs (crystal, switching-regulator inductor) as in-memory rule areas")
      ->check(CLI::IsMember({"off", "report", "soft", "on"}));
  route->add_option("--rules-override", r_job.rules_override,
                    "User override file for --component-rules (JSON, doc 15 §6.3): disable rules, assert/deny categories, set parameters")
      ->check(CLI::ExistingFile);
  route->add_option("--emit-items", r_items, "Write the new tracks and vias as JSON (for the KiCad plugin)");
  route->footer("Exit codes: 0 every planned connection routed (not KiCad DRC sign-off); "
                "3 output written but connections remain unrouted; 1 runtime error. "
                "CLI usage errors return CLI11's nonzero usage status.");
  route->callback([&job, state] {
    job.opt.pitch = static_cast<Coord>(state->pitch_um * 1000.0);
    job.opt.pair_skew = static_cast<Coord>(state->pair_skew_mm * 1e6);
    job.opt.gpu_device = default_gpu_device(job.use_gpu);
    if (state->no_kb) job.kb_path.clear();
  });
}

RouteJob parse_route_args(RouteJob job, const std::vector<std::string>& args) {
  CLI::App app{"TraceMaker route options"};
  add_route_options(app, job);
  std::vector<const char*> argv{"route"};
  for (const auto& arg : args) argv.push_back(arg.c_str());
  try {
    app.parse(static_cast<int>(argv.size()), argv.data());
  } catch (const CLI::ParseError& e) {
    throw std::invalid_argument(e.what());
  }
  return job;
}

RouteJobResult run_route_job(RouteJob job) {
  auto log = [&](const std::string& line) {
    if (job.log) job.log(line);
  };
  auto& opt = job.opt;
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
  if (!job.no_tracks_on.empty() || !job.layer_costs.empty()) {
    const auto& bd = lb.board;
    const auto lim = route::layer_limits(bd, job.no_tracks_on, job.layer_costs);
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
  // Knowledge base (failure memory T3): earlier failures on this board go first; variant choice by bandit.
  std::unique_ptr<learn::KnowledgeBase> kb;
  if (!job.kb_path.empty()) {
    kb = std::make_unique<learn::KnowledgeBase>(job.kb_path);
    if (!kb->ok()) kb.reset();
  }
  int conn_estimate = 0;
  for (const auto& n : lb.board.nets) conn_estimate += !n.name.empty();
  const auto feat = learn::features_of(lb.board, lb.doc.text(), conn_estimate);
  if (kb) {
    for (const auto& f : kb->failed_connections(feat.hash)) opt.priority.emplace_back(f.pad_a, f.pad_b);
    if (!opt.priority.empty()) log(fmt("knowledge base: %zu connections that failed before are routed first", opt.priority.size()));
  }
  // Component rules (doc 15 §5.5): generated keep-outs are added to an in-memory copy of the board the router
  // sees; the output is written from the original document, so the user's board never gains them.
  const crules::Mode cr_mode = crules::parse_mode(job.component_rules);
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
  // Custom rules the router cannot apply (unreadable conditions, position-dependent disallow rules) are named
  // here, not only by `tracemaker drc`: the routed board may break them (rule 6).
  {
    const drc::RuleEngine re(*route_board, rules);
    for (const auto& w : re.warnings()) {
      log("warning: " + w);
      rule_warnings.push_back(w);
    }
  }
  auto& res = out.result;
  std::vector<int> ran;
  int best_index = 0;
  std::string best_name;
  // The variant set is a setting, never derived from the thread count when a work budget makes the run
  // deterministic: then `--threads` only changes how fast the same variants finish (requirement N3, D47).
  const int threads = std::max(1, job.threads);
  const int variants = std::clamp(job.variants > 0 ? job.variants : opt.work_budget > 0 ? route::portfolio_size() : threads, 1,
                                  route::portfolio_size());
  if (variants > 1) {
    std::vector<int> pick;
    if (kb && variants < route::portfolio_size()) pick = kb->choose_variants(feat, route::portfolio_size(), variants, opt.seed);
    log(fmt("portfolio: %d variants on %d thread%s", variants, std::min(threads, variants), std::min(threads, variants) == 1 ? "" : "s"));
    auto pr = route::route_portfolio(*route_board, rules, opt, variants, pick, threads);
    for (std::size_t i = 0; i < pr.variants.size(); ++i)
      log(fmt("  variant %d %-30s routed %d in %.1f s%s", pr.indices[i], pr.variants[i].c_str(), pr.routed[i], pr.seconds[i],
              static_cast<int>(i) == pr.best_variant ? "  <- best" : ""));
    ran = pr.indices;
    best_index = pr.indices[static_cast<std::size_t>(pr.best_variant)];
    best_name = pr.variants[static_cast<std::size_t>(pr.best_variant)];
    res = std::move(pr.best);
  } else {
    res = route::Router(*route_board, rules, opt).run();
    ran = {0};
  }
  if (kb) {
    kb->record_run(feat, name, ran, best_index, res.routed, res.connections, res.seconds);
    std::vector<learn::FailedConnection> failed;
    for (const auto& u : res.unrouted) failed.push_back({u.net, u.a, u.b, 1});
    kb->record_failures(feat.hash, failed);
  }
  if (!job.out.empty()) {
    io::BoardEditor ed(lb, opt.seed);
    for (const auto& t : res.tracks) ed.add_track(t);
    for (const auto& v : res.vias) ed.add_via(v);
    ed.save(job.out);
  }
  log(fmt("routed %d/%d connections, %zu tracks, %zu vias, pitch %.3f mm, %ld expansions, %.2f s", res.routed, res.connections, res.tracks.size(),
          res.vias.size(), nm_to_mm(res.pitch), res.expansions, res.seconds));
  for (const auto& f : res.failures) log("  unrouted: " + f);
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
                 {"vias", res.vias.size()},  {"seconds", res.seconds},         {"expansions", res.expansions},
                 {"pitch_mm", nm_to_mm(res.pitch)}, {"failures", res.failures}, {"variant", best_index}, {"variant_name", best_name},
                 {"escape_corridors", res.escape_corridors}};
  out.summary["rules"] = {{"project", sources.project}, {"dru", sources.dru}, {"warnings", rule_warnings},
                          {"project_loaded", std::filesystem::exists(sources.project)}, {"dru_loaded", std::filesystem::exists(sources.dru)}};
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
  if (!cr_classes.empty()) out.summary["component_rule_net_classes"] = cr_classes;
  // Differential pairs (doc 05 §15): how each wanted pair came out, measured on the new copper (only when pairs are on).
  if (opt.diff_pairs || !opt.pair_nets.empty()) {
    const drc::RuleEngine re(lb.board, rules);
    auto want = opt.diff_pairs ? route::named_pairs(lb.board, re) : std::vector<std::pair<model::NetId, model::NetId>>{};
    for (const auto& p : opt.pair_nets)
      if (std::find(want.begin(), want.end(), p) == want.end() && std::find(want.begin(), want.end(), std::make_pair(p.second, p.first)) == want.end())
        want.push_back(p);
    nlohmann::json pj = nlohmann::json::array();
    log(fmt("differential pairs: %zu wanted, %d coupled pair routes (re-coupling included), %ld work units", want.size(), res.pairs, res.pair_work));
    out.summary["pair_work"] = res.pair_work;
    for (const auto& [na, nb] : want) {
      const auto pr = route::pair_rule(lb.board, rules, re, na, nb);
      const auto st = route::measure_pair(res.tracks, res.vias, na, nb, route::coupled_threshold(pr));
      const std::string& a = lb.board.nets[static_cast<std::size_t>(na)].name;
      const std::string& b = lb.board.nets[static_cast<std::size_t>(nb)].name;
      log(fmt("  %s / %s: %.1f mm, coupled %.0f %%, gap %.3f mm (target %.3f, %s), skew %.3f mm, vias %d/%d", a.c_str(), b.c_str(),
              (st.length_a + st.length_b) / 2e6, 100 * st.coupled_share(), st.gap_median / 1e6, nm_to_mm(pr.gap), pr.source.c_str(), st.skew() / 1e6,
              st.vias_a, st.vias_b));
      pj.push_back({{"net_a", a}, {"net_b", b}, {"coupled_share", st.coupled_share()}, {"gap_mm", st.gap_median / 1e6}, {"target_gap_mm", nm_to_mm(pr.gap)},
                    {"skew_mm", st.skew() / 1e6}, {"length_mm", (st.length_a + st.length_b) / 2e6}});
    }
    out.summary["pairs"] = pj;
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
