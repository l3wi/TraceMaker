// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
// Routing intent manifest (doc 05 §45, D110): the electrical intent the router must keep, as a reviewed file.
//
// Deciding the intent (which capacitor decouples which pin, which nets must not branch) is board preparation:
// `draft_intent` proposes it from the component rules (doc 15), a person or an LLM following
// skills/tracemaker-board-prep reviews and edits the file, and the router applies it exactly and deterministically
// (`tracemaker route --intent FILE`). The file never changes routing unless it is passed.
//
//   {"version": 1, "board": "x.kicad_pcb",
//    "connections":    [{"from": "C3.1", "to": "U1.7", "why": "100 nF decoupling of U1 VDD"}],
//    "plane_pads":     [{"pad": "C3.2", "why": "decap ground: own via"}],
//    "no_branch_nets": [{"net": "/USB_D+", "why": "USB 2.0 data: no stubs"}]}
//
//   connections     each pair is routed as its own pad-to-pad connection (unless fixed copper already joins them),
//                   and neither end may start or end on other copper of the net (no tree attach, no plane tie)
//   plane_pads      the pad gets its own connection into a plane fill of its net, from the pad itself, when the net
//                   has one the pad can reach and fixed copper does not already join it
//   no_branch_nets  no connection of the net starts or ends on copper other than its own pads or plane
#include <string>
#include <utility>
#include <vector>

#include <nlohmann/json.hpp>

#include "crules/catalogue.hpp"
#include "crules/detect.hpp"
#include "model/board.hpp"

namespace tmk::crules {

struct Intent {
  struct Pair { std::string from, to, why; };
  struct PlanePad { std::string pad, why; };
  struct Net { std::string net, why; };
  std::string board, comment;
  std::vector<Pair> connections;
  std::vector<PlanePad> plane_pads;
  std::vector<Net> no_branch_nets;
};

// Strict: unknown keys, wrong types, empty names and a version other than 1 throw std::runtime_error naming
// `source` and the entry.
Intent parse_intent(const std::string& text, const std::string& source);
Intent load_intent_file(const std::string& path);
nlohmann::ordered_json intent_json(const Intent& in);

// Board pad indices and net ids. Throws std::runtime_error for an unknown or ambiguous reference ("REF.NUM": the
// reference must name one footprint and the number one pad of it), an unknown net, a pad without a net, a
// connection whose pads are on different nets or the same pad twice.
struct ResolvedIntent {
  std::vector<std::pair<int, int>> pairs;
  std::vector<int> plane_pads;
  std::vector<model::NetId> no_branch_nets;
};
ResolvedIntent resolve_intent(const model::Board& b, const Intent& in, const std::string& source);

// A draft from the component rules, for review (instances at the apply threshold, rules not disabled by the user,
// parts with a unique reference and pads with a unique number only):
//   ic_decoupling (DEC-01, DEC-03): capacitor supply pad -> bound IC pin; both capacitor pads are plane pads;
//   point-to-point interfaces (USB, USB-C, Ethernet, HDMI/DP, LVDS, MIPI, PCIe, SATA, crystal, RF line): nets bound to
//   the roles of their max_stub, diff_pair and via_limit rules are no-branch nets.
// Every entry's `why` names the rule, the parts and the detection confidence. Catalogue then reference order.
Intent draft_intent(const model::Board& b, const Catalogue& cat, const Detection& det);

}  // namespace tmk::crules
