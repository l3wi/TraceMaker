// SPDX-License-Identifier: GPL-3.0-or-later
// Routing intent manifest (doc 05 §44, D103): reading, resolving against a board, and the component-rule draft.
#include "crules/intent.hpp"

#include <algorithm>
#include <fstream>
#include <map>
#include <set>
#include <sstream>
#include <stdexcept>

namespace tmk::crules {

namespace {

using nlohmann::ordered_json;

[[noreturn]] void fail(const std::string& source, const std::string& msg) { throw std::runtime_error(source + ": " + msg); }

void check_keys(const std::string& source, const std::string& where, const ordered_json& obj, const std::set<std::string>& allowed) {
  if (!obj.is_object()) fail(source, where + ": expected a map");
  for (const auto& [k, v] : obj.items()) {
    (void)v;
    if (allowed.count(k)) continue;
    std::string list;
    for (const auto& a : allowed) list += (list.empty() ? "" : ", ") + a;
    fail(source, where + ": unknown key '" + k + "' (allowed: " + list + ")");
  }
}

std::string need_string(const std::string& source, const std::string& where, const ordered_json& obj, const char* key) {
  if (!obj.contains(key) || !obj[key].is_string() || obj[key].get<std::string>().empty())
    fail(source, where + ": '" + key + "' must be a non-empty string");
  return obj[key].get<std::string>();
}

std::string opt_string(const std::string& source, const std::string& where, const ordered_json& obj, const char* key) {
  if (!obj.contains(key)) return {};
  if (!obj[key].is_string()) fail(source, where + ": '" + key + "' must be a string");
  return obj[key].get<std::string>();
}

const ordered_json& list_of(const std::string& source, const ordered_json& j, const char* key) {
  static const ordered_json empty = ordered_json::array();
  if (!j.contains(key)) return empty;
  if (!j[key].is_array()) fail(source, std::string(key) + ": expected a list");
  return j[key];
}

// "REF.NUM" -> pad index. The split is at the '.' after which the reference names a footprint, so references and pad
// numbers may themselves contain dots as long as exactly one split works.
int resolve_pad(const model::Board& b, const std::multimap<std::string, int>& by_ref, const std::string& source, const std::string& where,
                const std::string& name) {
  int found = -1;
  for (std::size_t dot = name.find('.'); dot != std::string::npos; dot = name.find('.', dot + 1)) {
    const std::string ref = name.substr(0, dot), num = name.substr(dot + 1);
    if (by_ref.count(ref) == 0) continue;
    if (by_ref.count(ref) > 1) fail(source, where + ": reference " + ref + " names " + std::to_string(by_ref.count(ref)) + " footprints");
    const auto& fp = b.footprints[static_cast<std::size_t>(by_ref.find(ref)->second)];
    int pad = -1;
    for (const int p : fp.pads)
      if (b.pads[static_cast<std::size_t>(p)].number == num) {
        if (pad >= 0) fail(source, where + ": " + ref + " has more than one pad " + num);
        pad = p;
      }
    if (pad < 0) fail(source, where + ": " + ref + " has no pad " + num);
    if (found >= 0 && found != pad) fail(source, where + ": '" + name + "' is ambiguous");
    found = pad;
  }
  if (found < 0) fail(source, where + ": no footprint for '" + name + "' (expected REF.NUM)");
  return found;
}

std::string pad_name(const model::Board& b, int pad) {
  const auto& p = b.pads[static_cast<std::size_t>(pad)];
  return b.footprints[static_cast<std::size_t>(p.footprint)].reference + "." + p.number;
}

}  // namespace

Intent parse_intent(const std::string& text, const std::string& source) {
  ordered_json j;
  try {
    j = ordered_json::parse(text);
  } catch (const std::exception& e) {
    fail(source, std::string("not valid JSON: ") + e.what());
  }
  check_keys(source, "top level", j, {"version", "board", "comment", "connections", "plane_pads", "no_branch_nets"});
  if (!j.contains("version") || !j["version"].is_number_integer() || j["version"].get<int>() != 1) fail(source, "version must be 1");
  Intent in;
  in.board = opt_string(source, "top level", j, "board");
  in.comment = opt_string(source, "top level", j, "comment");
  const auto& cs = list_of(source, j, "connections");
  for (std::size_t i = 0; i < cs.size(); ++i) {
    const std::string where = "connections[" + std::to_string(i) + "]";
    check_keys(source, where, cs[i], {"from", "to", "why"});
    in.connections.push_back({need_string(source, where, cs[i], "from"), need_string(source, where, cs[i], "to"), opt_string(source, where, cs[i], "why")});
  }
  const auto& ps = list_of(source, j, "plane_pads");
  for (std::size_t i = 0; i < ps.size(); ++i) {
    const std::string where = "plane_pads[" + std::to_string(i) + "]";
    check_keys(source, where, ps[i], {"pad", "why"});
    in.plane_pads.push_back({need_string(source, where, ps[i], "pad"), opt_string(source, where, ps[i], "why")});
  }
  const auto& ns = list_of(source, j, "no_branch_nets");
  for (std::size_t i = 0; i < ns.size(); ++i) {
    const std::string where = "no_branch_nets[" + std::to_string(i) + "]";
    check_keys(source, where, ns[i], {"net", "why"});
    in.no_branch_nets.push_back({need_string(source, where, ns[i], "net"), opt_string(source, where, ns[i], "why")});
  }
  return in;
}

Intent load_intent_file(const std::string& path) {
  std::ifstream f(path, std::ios::binary);
  if (!f) throw std::runtime_error("cannot read intent file " + path);
  std::stringstream ss;
  ss << f.rdbuf();
  return parse_intent(ss.str(), path);
}

ordered_json intent_json(const Intent& in) {
  ordered_json j;
  j["version"] = 1;
  if (!in.board.empty()) j["board"] = in.board;
  if (!in.comment.empty()) j["comment"] = in.comment;
  auto with_why = [](ordered_json e, const std::string& why) {
    if (!why.empty()) e["why"] = why;
    return e;
  };
  j["connections"] = ordered_json::array();
  for (const auto& c : in.connections) j["connections"].push_back(with_why({{"from", c.from}, {"to", c.to}}, c.why));
  j["plane_pads"] = ordered_json::array();
  for (const auto& p : in.plane_pads) j["plane_pads"].push_back(with_why({{"pad", p.pad}}, p.why));
  j["no_branch_nets"] = ordered_json::array();
  for (const auto& n : in.no_branch_nets) j["no_branch_nets"].push_back(with_why({{"net", n.net}}, n.why));
  return j;
}

ResolvedIntent resolve_intent(const model::Board& b, const Intent& in, const std::string& source) {
  std::multimap<std::string, int> by_ref;
  for (std::size_t f = 0; f < b.footprints.size(); ++f) by_ref.emplace(b.footprints[f].reference, static_cast<int>(f));
  auto netted = [&](const std::string& where, const std::string& name) {
    const int p = resolve_pad(b, by_ref, source, where, name);
    if (b.pads[static_cast<std::size_t>(p)].net == 0) fail(source, where + ": pad " + name + " has no net");
    return p;
  };
  ResolvedIntent r;
  for (std::size_t i = 0; i < in.connections.size(); ++i) {
    const std::string where = "connections[" + std::to_string(i) + "]";
    const int a = netted(where + ".from", in.connections[i].from), c = netted(where + ".to", in.connections[i].to);
    if (a == c) fail(source, where + ": from and to are the same pad");
    const auto na = b.pads[static_cast<std::size_t>(a)].net, nc = b.pads[static_cast<std::size_t>(c)].net;
    if (na != nc)
      fail(source, where + ": " + in.connections[i].from + " is on " + b.nets[static_cast<std::size_t>(na)].name + " but " + in.connections[i].to + " is on " +
                       b.nets[static_cast<std::size_t>(nc)].name);
    r.pairs.emplace_back(a, c);
  }
  for (std::size_t i = 0; i < in.plane_pads.size(); ++i) r.plane_pads.push_back(netted("plane_pads[" + std::to_string(i) + "]", in.plane_pads[i].pad));
  for (std::size_t i = 0; i < in.no_branch_nets.size(); ++i) {
    const auto n = b.net_by_name(in.no_branch_nets[i].net);
    if (n == 0) fail(source, "no_branch_nets[" + std::to_string(i) + "]: no net '" + in.no_branch_nets[i].net + "' on the board");
    r.no_branch_nets.push_back(n);
  }
  return r;
}

Intent draft_intent(const model::Board& b, const Catalogue& cat, const Detection& det) {
  std::map<std::string, int> ref_count;
  for (const auto& fp : b.footprints) ++ref_count[fp.reference];
  // A pad the manifest can name: unique reference, unique number within its footprint.
  auto nameable = [&](int pad) {
    const auto& p = b.pads[static_cast<std::size_t>(pad)];
    const auto& fp = b.footprints[static_cast<std::size_t>(p.footprint)];
    if (ref_count[fp.reference] != 1) return false;
    return std::count_if(fp.pads.begin(), fp.pads.end(), [&](int q) { return b.pads[static_cast<std::size_t>(q)].number == p.number; }) == 1;
  };
  // An inner pin of a pad array (a ball with neighbours left, right, above and below in its own row and column): a
  // capacitor reaches it through its fan-out via, not by a pad-to-pad track, so no pair is drafted for it (on `mid`
  // sbc 45 drafted capacitor-to-ball pairs were unroutable and starved other nets).
  auto inner_pin = [&](int pad) {
    const auto& p = b.pads[static_cast<std::size_t>(pad)];
    constexpr Coord kBand = 100'000;  // 0.1 mm: same row or column
    bool left = false, right = false, above = false, below = false;
    for (const int q : b.footprints[static_cast<std::size_t>(p.footprint)].pads) {
      const Point o = b.pads[static_cast<std::size_t>(q)].pos;
      if (std::abs(o.y - p.pos.y) < kBand) {
        left |= o.x < p.pos.x - kBand;
        right |= o.x > p.pos.x + kBand;
      }
      if (std::abs(o.x - p.pos.x) < kBand) {
        above |= o.y < p.pos.y - kBand;
        below |= o.y > p.pos.y + kBand;
      }
    }
    return left && right && above && below;
  };
  Intent out;
  std::set<std::pair<int, int>> seen_pairs;
  std::set<int> seen_pads;
  std::set<model::NetId> seen_nets;
  auto plane_pad = [&](int pad, const std::string& why) {
    if (nameable(pad) && b.pads[static_cast<std::size_t>(pad)].net != 0 && seen_pads.insert(pad).second) out.plane_pads.push_back({pad_name(b, pad), why});
  };
  for (const auto& in : det.instances) {
    if (in.confidence < cat.apply) continue;
    const auto& C = cat.categories[static_cast<std::size_t>(in.category)];
    const std::string conf = " (component rules, confidence " + std::to_string(in.confidence) + ")";
    if (C.id == "ic_decoupling" && !in.disabled("DEC-01")) {
      const Role* cap = in.role("cap");
      const Role* pin = in.role("pin");
      if (cap && pin && !cap->pads.empty() && !pin->pads.empty()) {
        const int cp = cap->pads.front(), ip = pin->pads.front();
        const std::string what = pad_name(b, cp) + " decouples " + pad_name(b, ip) + " (" + b.nets[static_cast<std::size_t>(b.pads[static_cast<std::size_t>(cp)].net)].name + ")";
        if (nameable(cp) && nameable(ip) && !inner_pin(ip) && seen_pairs.insert({cp, ip}).second)
          out.connections.push_back({pad_name(b, cp), pad_name(b, ip), "DEC-01 " + what + conf});
        if (!in.disabled("DEC-03")) {
          // Via at the capacitor (DEC-03: plane via, capacitor, pin): its supply pad and its other pads.
          plane_pad(cp, "DEC-03 plane via at " + pad_name(b, cp) + conf);
          for (const int p : b.footprints[static_cast<std::size_t>(in.anchor)].pads)
            if (p != cp && b.pads[static_cast<std::size_t>(p)].net != b.pads[static_cast<std::size_t>(cp)].net)
              plane_pad(p, "DEC-03 return via at " + pad_name(b, p) + conf);
        }
      }
      continue;
    }
    // Point-to-point high-speed and clock interfaces only: power paths, ESD-protected nets, CAN/RS-485 buses (multi-
    // drop), motor gates and analog inputs carry diff_pair/via_limit rules too but may branch.
    static const std::set<std::string> kLines = {"usb2", "usb3_typec", "ethernet", "hdmi_dp", "lvds", "mipi_dphy", "pcie", "sata", "crystal", "rf_line"};
    if (!kLines.count(C.id)) continue;
    for (const auto& r : C.rules) {
      if ((r.kind != "max_stub" && r.kind != "diff_pair" && r.kind != "via_limit") || in.disabled(r.id)) continue;
      for (const auto& role : r.applies_to)
        if (const Role* ro = in.role(role))
          for (const auto n : ro->nets)
            if (n != 0 && seen_nets.insert(n).second)
              out.no_branch_nets.push_back({b.nets[static_cast<std::size_t>(n)].name,
                                            r.id + " " + C.id + " " + role + " of " + b.footprints[static_cast<std::size_t>(in.anchor)].reference + conf});
    }
  }
  return out;
}

}  // namespace tmk::crules
