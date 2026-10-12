// SPDX-License-Identifier: GPL-3.0-or-later
#include "app/route_job.hpp"
#include "app/tui.hpp"

#include <CLI/CLI.hpp>
#include <algorithm>
#include <fstream>
#include <iterator>
#include <memory>
#include <stdexcept>

namespace tmk::app {
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
  route->add_option("--config", job.config,
                    "Read options from a file of `name = value` lines, as `tracemaker tui` saves them; options on the command line win")
      ->check(CLI::ExistingFile);
  auto* output = route->add_option("-o,--output", job.out, "Output .kicad_pcb");
  if (require_paths) board->required(), output->required();
  route->add_option("--project", job.project, "Read this project and its same-stem .kicad_dru instead of the board's stem");
  route->add_option("--time", ropt.time_limit_s, "Time limit, seconds (safety net; results then depend on machine speed)");
  route->add_option("--work", ropt.work_budget,
                    "Deterministic work budget in search expansions per router (e.g. 50000000); with the default portfolio every "
                    "variant gets the whole budget, so a run does up to --variants times this work");
  route->add_option("--pitch-um", r_pitch_um, "Lattice pitch in micrometres (default: automatic)");
  route->add_option("--guide", job.guide, "Preferred paths per connection (JSON, Simple Route JSON coordinates in mm; doc 16 F1)")->group("");
  route->add_option("--guide-pen", ropt.guide_pen, "Guide: pitches per lattice step off the guide")->group("");
  route->add_option("--guide-radius", ropt.guide_radius, "Guide: pitches around the guide that count as on it")->group("");
  route->add_option("--via-cost-mm", ropt.via_cost_mm, "Cost of a via as equivalent track length");
  route->add_option("--order", ropt.order, "Connection order: 0 shortest first, 1 longest first, 2 jittered, 3 deep package pins first")->group("");
  route->add_option("--seed", ropt.seed);
  route->add_option("--only-net", ropt.only_net, "Debugging: route only this net")->group("");
  route->add_option("--skip-net", ropt.skip_nets, "Experiments: leave this net unrouted (repeatable)")->group("");
  route->add_flag("!--no-strict-first", ropt.strict_first, "Negotiate from the first pass (no strict-only pass)")->group("");
  route->add_option("--polish", ropt.polish_fraction, "Reserve this share of the budget (0-0.9) to rebuild the open connections of the best state")->group("");
  route->add_option("--polish-trials", ropt.polish_trials, "Polish: seeded orders tried per open connection")->group("");
  route->add_option("--lns-open", ropt.lns_open_trials, "Regional rebuild of open connections: orders tried per open connection (0 = off)")->group("");
  route->add_option("--lns-open-radius-mm", ropt.lns_open_radius_mm, "Regional rebuild: radius around the open pin")->group("");
  route->add_flag("--layer-assign", ropt.layer_assign, "Prefer per connection the layer with the fewest crossings (two-layer boards; doc 16 F2)");
  route->add_option("--layer-dir-pen", ropt.layer_dir_pen,
                    "Cost (pitches per step) of orthogonal steps against the layer's preferred direction (even layers horizontal, odd vertical)")->group("");
  route->add_flag("--layer-dir-swap", ropt.layer_dir_swap, "Preferred directions the other way round (even layers vertical)")->group("");
  route->add_option("--package-pen", ropt.package_pen,
                    "Extra cost (pitches per step) of tracks on the pad layer inside a ball-grid package: escapes take a via early")->group("");
  route->add_flag("--layer-assign-angle", ropt.layer_assign_angle, "Layer preference per connection by chord direction (two-layer boards)")->group("");
  route->add_option("--layer-assign-pen", ropt.layer_assign_pen, "Layer assignment: pitches per lattice step on the other layer")->group("");
  route->add_option("--history-weight", ropt.history_weight, "Negotiation: pitches of cost per recorded conflict on a cell (default 2)")->group("");
  route->add_option("--max-victims", ropt.max_victims, "Negotiation: connections one route may rip at once (0 = no limit)")->group("");
  route->add_option("--max-rips", ropt.max_rips_per_connection, "Rip-ups allowed per connection")->group("");
  route->add_option("--max-passes", ropt.max_passes, "Passes over still-unrouted connections")->group("");
  route->add_option("--max-restarts", ropt.max_restarts, "Full restarts when negotiation stalls")->group("");
  route->add_option("--soft-growth", ropt.soft_growth, "Crossing cost growth per negotiation pass (fraction of --soft-cost-mm)")->group("");
  route->add_option("--soft-cost-mm", ropt.soft_cost_mm, "Base cost of crossing another net's copper in negotiation")->group("");
  route->add_option("--soft-attempts", ropt.soft_attempts, "Window sizes tried by negotiated searches (1-4)")->group("");
  route->add_option("--heuristic-weight", ropt.heuristic_weight, "Weighted A* factor (1.0 = optimal searches)");
  route->add_flag("!--no-rip-up", ropt.rip_up, "Disable negotiated rip-up and reroute");
  route->add_flag("--micro-vias", ropt.micro_vias, "Use micro vias (outer layer to the next) where a through via is blocked (only on boards that allow them)");
  route->add_flag("--blind-vias", ropt.blind_vias, "Use blind/buried vias where a through via is blocked (only on boards that allow them)");
  route->add_flag("--diff-pairs", ropt.diff_pairs, "Route differential pairs (KiCad P/N or +/- names) as coupled pairs first (doc 05 §15)");
  route->add_flag("--pair-twists,!--no-pair-twists", ropt.pair_twists, "Coupled pairs may swap sides at a layer change, for pairs whose pin order is mirrored between the ends (default on; only with pair routing)");
  route->add_option("--pair-max-expansions", ropt.pair_max_expansions, "Local coupled-pair state cap (0 = length-scaled 40k-250k; ablation only)")
      ->check(CLI::NonNegativeNumber)->group("");
  route->add_flag("!--no-pair-leg-cache", ropt.pair_leg_cache, "Disable local exact-leg reuse (reference/ablation only)")->group("");
  route->add_flag("!--no-pair-exit-index", ropt.pair_exit_index, "Disable exit-guidance index and memo (reference/ablation only)")->group("");
  route->add_option("--pair-skew-mm", r_pair_skew_mm, "Intra-pair skew limit for coupled pairs: meanders on the shorter half (0 = custom skew rules only)");
  route->add_flag("--global", ropt.global_route, "Global routing first: detailed search follows coarse corridors");
  route->add_flag("--global-confine", ropt.global_confine, "With --global: confine each connection's first search to its corridor (experimental)")->group("");
  route->add_flag("--global-corridor-only", ropt.global_strict_corridor_only, "With --global-confine: a corridor failure goes straight to negotiation (experimental)")->group("");
  route->add_option("--max-expansions", ropt.max_expansions, "Search expansions per attempt")->group("");
  route->add_flag("!--no-optimize", ropt.optimize, "Skip the post-routing clean-up pass (fewer vias, shorter tracks)");
  route->add_flag("--plan-gap,!--no-plan-gap", ropt.plan_gap, "Plan connections by copper-to-copper distance between pads rather than centre to centre (default on)");
  route->add_flag("--tree-attach,!--no-tree-attach", ropt.tree_attach,
                  "Connections may start or end on any copper already joined to their pads (branch off the net's tree)");
  route->add_flag("--plane-tie,!--no-plane-tie", ropt.plane_tie,
                  "A pad's plane connection may end on nearby copper of its net that already reaches the plane instead of its own via");
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
  route->add_flag("!--no-refill-repair", job.refill_repair,
                  "With --soft-zones: only count what the zone refill leaves unconnected, do not route it");
  route->add_flag("--via-in-pad", ropt.via_in_pad, "Inner balls / enclosed SMD pads may take a minimum-size via in the pad (needs filled, capped vias)");
  route->add_flag("--keep-vias-off-pads", ropt.vias_off_pads, "Vias keep clear of SMD pads narrower than --vias-off-pads-below (via-in-pad excepted)");
  route->add_option("--vias-off-pads-below", ropt.vias_off_pads_below_mm, "Pad width (mm) below which --keep-vias-off-pads applies (default 2)");
  route->add_option("--first-nets", ropt.first_nets, "Comma-separated nets routed first, also after restarts; other nets do not rip them (e.g. crystal lines)")->delimiter(',');
  route->add_option("--bus", job.buses,
                    "Experimental. Route these nets as a bus, NAME=PATTERN (repeatable): lane by lane on the same layers, e.g. 'DATA=/D[0-7]' or 'LCD=/LCD_*'. "
                    "PATTERN matches whole net names: * and ? as in net-class patterns, [N-M] a number in that range (doc 05 §38)");
  route->add_flag("--buses", job.detect_buses,
                  "Experimental. Also find buses in the net names: nets differing only in a trailing index, four or more between the same two components");
  route->add_flag("--bus-first", ropt.bus_first, "Route the buses before everything else (default: each bus where the router's order puts its first connection)");
  route->add_option("--bus-skew-mm", job.bus_skew_mm,
                    "Length-match the --bus groups: meanders until their lengths (tracks and via height, as KiCad's skew check) differ by at most this "
                    "(0 = only the board's custom skew rules)");
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
  route->add_flag("--protect-planes", r_job.protect_planes,
                  "No new tracks on inner layers where one net's zone outlines cover at least 60% of the board (vias still reach planes)");
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
  route->add_option("--intent", r_job.intent,
                    "Routing intent manifest (JSON, doc 05 §45): pad pairs routed as their own connections, pads with their own plane "
                    "via, nets that never branch; draft one with `tracemaker rules BOARD --intent-draft FILE` and review it")
      ->check(CLI::ExistingFile);
  route->add_option("--emit-items", r_items, "Write the new tracks and vias as JSON (for the KiCad plugin)");
  route->add_flag("--widen-power", job.widen_power,
                  "After routing: widen new tracks that are narrower than their net class width back up to it where there is room, and nets "
                  "with a target (--widen-net, or a width the component rules computed) towards the target (doc 05 §39)");
  route->add_option("--widen-net", job.widen_nets,
                    "With --widen-power: target width for nets as PATTERN=WIDTH_MM, e.g. '+5V=0.8' or 'VBUS*=1' (repeatable)");
  route->add_option("--stitch-vias", job.stitch_vias,
                    "After routing: add stitching vias. planes: vias of a plane's net on a grid where its fills overlap on two or more layers; "
                    "return: a plane via near each via of the nets that need a reference plane (component rules, --stitch-return); all: both")
      ->check(CLI::IsMember({"planes", "return", "all"}));
  route->add_option("--stitch-pitch", job.stitch_pitch_mm, "Grid pitch of plane stitching vias, mm (default 5.08, the catalogue's value)")
      ->check(CLI::PositiveNumber);
  route->add_option("--stitch-distance", job.stitch_distance_mm,
                    "Return-path stitching: largest distance from a signal via to its plane via, mm (default: the rule's, else 5.08)")
      ->check(CLI::PositiveNumber);
  route->add_option("--stitch-return", job.stitch_returns,
                    "Return-path stitching for nets as PATTERN=PLANE_NET, e.g. 'USB_D*=GND' (repeatable). Return-path stitching from component rules is experimental");
  route->add_option("--pass-work", job.pass_work, "Work budget of each after-route pass, in exact checks (default 2,000,000)")->check(CLI::PositiveNumber);
  route->footer("Exit codes: 0 every planned connection routed and, with soft zones, no unconnected items after refill (not KiCad DRC sign-off); "
                "3 output written but connections or refill-judged unconnected items remain; 1 runtime error. "
                "CLI usage errors return CLI11's nonzero usage status.");
  route->callback([&job, state] {
    job.opt.pitch = static_cast<Coord>(state->pitch_um * 1000.0);
    job.opt.pair_skew = static_cast<Coord>(state->pair_skew_mm * 1e6);
    job.opt.gpu_device = default_gpu_device(job.use_gpu);
    if (state->no_kb) job.kb_path.clear();
  });
}

RouteJob parse_route_args(RouteJob job, const std::vector<std::string>& args) {
  const RouteJob initial = job;
  CLI::App app{"TraceMaker route options"};
  add_route_options(app, job);
  std::vector<const char*> argv{"route"};
  for (const auto& arg : args) argv.push_back(arg.c_str());
  try {
    app.parse(static_cast<int>(argv.size()), argv.data());
    if (!job.config.empty()) {
      auto all = route_config_args(app, job.config);
      all.insert(all.end(), args.begin(), args.end());
      job = initial;
      app.clear();
      argv = {"route"};
      for (const auto& arg : all) argv.push_back(arg.c_str());
      app.parse(static_cast<int>(argv.size()), argv.data());
    }
  } catch (const CLI::ParseError& e) {
    throw std::invalid_argument(e.what());
  }
  return job;
}
// The arguments an options file stands for (`route --config`, doc 02 §2.1): one `name = value` per line. An option
// that is also on the command line is left out, so the command line wins. Throws on a line that names no option.
std::vector<std::string> route_config_args(CLI::App& cmd, const std::string& path) {
  std::ifstream f(path);
  if (!f) throw std::runtime_error("cannot read the options file " + path);
  const std::string text((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
  std::vector<std::string> out;
  for (const auto& e : tmk::app::tui::parse_options(text)) {
    const std::string where = path + ":" + std::to_string(e.line) + ": ";
    CLI::Option* o = e.name.empty() || e.name == "config" ? nullptr : cmd.get_option_no_throw("--" + e.name);
    if (o == nullptr) throw std::runtime_error(where + "no route option named " + e.name);
    if (o->count() > 0) continue;
    if (o->get_expected_max() != 0) {
      out.push_back("--" + e.name + "=" + e.value);
      continue;
    }
    const int t = tmk::app::tui::truth(e.value);
    if (t < 0) throw std::runtime_error(where + e.name + " is a flag, its value must be true or false");
    if (t == 1) {
      out.push_back("--" + e.name);
      continue;
    }
    // "x = false": the flag's spelling of the other sense (--x against --no-x), if it has one.
    const auto& off = o->get_fnames();
    const bool negative = std::find(off.begin(), off.end(), e.name) != off.end();
    std::string other;
    for (const auto& n : o->get_lnames())
      if ((std::find(off.begin(), off.end(), n) != off.end()) != negative) other = n;
    if (other.empty()) throw std::runtime_error(where + e.name + " = false has no meaning: the flag has no opposite, remove the line");
    out.push_back("--" + other);
  }
  return out;
}

}  // namespace tmk::app
