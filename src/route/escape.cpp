// SPDX-License-Identifier: GPL-3.0-or-later
#include "route/escape.hpp"

#include "route/obstacles.hpp"
#include "drc/connectivity.hpp"

#include <algorithm>
#include <climits>
#include <cmath>
#include <cstdlib>
#include <deque>
#include <map>
#include <set>

namespace tmk::route {

namespace {

std::size_t z(int i) { return static_cast<std::size_t>(i); }

std::pair<Coord, Coord> half_extents(const model::Pad& p) { return pad_half_extents(p); }

}  // namespace

std::pair<Coord, Coord> pad_half_extents(const model::Pad& p) {
  const double a = p.angle * M_PI / 180.0;
  const double c = std::fabs(std::cos(a)), s = std::fabs(std::sin(a));
  const double hx = c * static_cast<double>(p.size_x) / 2 + s * static_cast<double>(p.size_y) / 2;
  const double hy = s * static_cast<double>(p.size_x) / 2 + c * static_cast<double>(p.size_y) / 2;
  return {static_cast<Coord>(std::llround(hx)), static_cast<Coord>(std::llround(hy))};
}

namespace {

int lowest_layer(model::LayerMask m) {
  for (int l = 0; l < 64; ++l)
    if (m & (model::LayerMask{1} << l)) return l;
  return -1;
}

}  // namespace

std::vector<EscapeCorridor> plan_escapes(const model::Board& b, const std::vector<char>& needs, const std::function<Coord(model::NetId)>& keep,
                                         const EscapeOptions& o, EscapeStats* stats, const std::function<Coord(model::NetId)>& channel) {
  std::vector<EscapeCorridor> out;
  for (const auto& fp : b.footprints) {
    std::vector<int> pads;
    for (int pi : fp.pads) {
      const auto& p = b.pads[z(pi)];
      if (p.copper != 0 && p.type != model::PadType::NpThruHole) pads.push_back(pi);
    }
    if (static_cast<int>(pads.size()) < o.min_pads) continue;
    // Pin pitch: the smallest centre distance between two pads of the footprint (pads at the same spot, e.g.
    // a thermal pad split into parts, do not count).
    Coord pitch = LLONG_MAX;
    for (std::size_t i = 0; i < pads.size(); ++i)
      for (std::size_t j = i + 1; j < pads.size(); ++j) {
        const Point d = b.pads[z(pads[j])].pos - b.pads[z(pads[i])].pos;
        const Coord dist = static_cast<Coord>(std::llround(std::hypot(static_cast<double>(d.x), static_cast<double>(d.y))));
        if (dist > 0) pitch = std::min(pitch, dist);
      }
    if (pitch == LLONG_MAX || pitch > o.max_pitch) continue;
    Coord x0 = LLONG_MAX, y0 = LLONG_MAX, x1 = LLONG_MIN, y1 = LLONG_MIN;
    for (int pi : pads) {
      const Point q = b.pads[z(pi)].pos;
      x0 = std::min(x0, q.x), x1 = std::max(x1, q.x), y0 = std::min(y0, q.y), y1 = std::max(y1, q.y);
    }
    const Coord ring = pitch * 3 / 4;  // within this of the pad-centre box: on the perimeter
    const Point centre{(x0 + x1) / 2, (y0 + y1) / 2};
    bool any = false;
    for (int pi : pads) {
      if (!needs[z(pi)]) continue;
      const auto& p = b.pads[z(pi)];
      if (p.type != model::PadType::Smd) continue;  // through-hole pins reach every layer already
      const int layer = lowest_layer(p.copper);
      if (layer < 0) continue;
      const Coord k = keep(p.net);
      const Point a = p.pos;
      const Coord dl = a.x - x0, dr = x1 - a.x, dt = a.y - y0, db = y1 - a.y;
      const Coord m = std::min({dl, dr, dt, db});
      EscapeCorridor c;
      c.pad = pi;
      c.net = p.net;
      c.layer = layer;
      c.a = a;
      if (m <= ring) {
        // Perimeter pin: straight out through the nearest side of the package (ties: left, right, top, bottom).
        const auto [hx, hy] = half_extents(p);
        if (m == dl) c.b = {a.x - hx - o.length, a.y};
        else if (m == dr) c.b = {a.x + hx + o.length, a.y};
        else if (m == dt) c.b = {a.x, a.y - hy - o.length};
        else c.b = {a.x, a.y + hy + o.length};
        c.band = std::min(k, pitch / 2 - 1);
        if (stats) ++stats->perimeter;
      } else if (channel && m <= pitch + ring && [&] {
                   const auto [hx, hy] = half_extents(p);
                   const Coord across = (m == dl || m == dr) ? 2 * hy : 2 * hx;  // pad extent along the outer row
                   return pitch - across >= channel(p.net);
                 }()) {
        // Second ring: diagonally to the interstitial site beside it (the same way for every ball of that half of
        // the side, so each gap between two outer balls serves one ball), then straight out between them.
        const auto [hx, hy] = half_extents(p);
        c.has_mid = true;
        if (m == dl || m == dr) {
          const Coord dir = m == dl ? -1 : 1, sj = a.y >= centre.y ? 1 : -1;
          c.mid = {a.x + dir * pitch / 2, a.y + sj * pitch / 2};  // the interstitial site, then out between the outer balls
          c.b = {a.x + dir * (pitch + hx + o.length), c.mid.y};
        } else {
          const Coord dir = m == dt ? -1 : 1, sj = a.x >= centre.x ? 1 : -1;
          c.mid = {a.x + sj * pitch / 2, a.y + dir * pitch / 2};
          c.b = {c.mid.x, a.y + dir * (pitch + hy + o.length)};
        }
        c.band = std::min(k, pitch * 35 / 100);
        if (stats) ++stats->second_ring;
      } else {
        // Inner ball: dog-bone to the diagonal interstitial site pointing away from the package centre, so all
        // balls of a quadrant fan out the same way and every site serves exactly one ball.
        const Coord sx = a.x >= centre.x ? 1 : -1, sy = a.y >= centre.y ? 1 : -1;
        c.b = {a.x + sx * pitch / 2, a.y + sy * pitch / 2};
        c.via = true;
        c.band = std::min(k, pitch * 35 / 100);
        if (stats) ++stats->dogbones;
      }
      if (c.band <= 0) continue;
      out.push_back(c);
      any = true;
    }
    if (any && stats) ++stats->parts;
  }
  return out;
}

namespace {
Coord analysis_pitch(const model::DesignRules& r, const EscapeAnalysisOptions& o) {
  if (o.routing.pitch > 0) return o.routing.pitch;
  Coord wc = 1'000'000'000;
  for (const auto& c : r.classes)
    wc = std::min(wc, std::max(c.track_width, r.minimums.track_width) + std::max(c.clearance, r.minimums.clearance));
  return std::clamp<Coord>(wc / 6 / 5'000 * 5'000, 25'000, 100'000);
}
}  // namespace
namespace {
std::uint64_t domain_hash(const model::Board& b, const model::DesignRules& r, const Obstacles& obs) {
  std::uint64_t hash = 1469598103934665603ull;
  auto value = [&](std::uint64_t v) { for (int i = 0; i < 8; ++i) { hash ^= v & 255; hash *= 1099511628211ull; v >>= 8; } };
  auto text = [&](const std::string& s) { value(s.size()); for (char c : s) { hash ^= static_cast<unsigned char>(c); hash *= 1099511628211ull; } };
  const auto& m = r.minimums;
  for (Coord v : {m.clearance, m.track_width, m.via_diameter, m.via_annular_width, m.through_hole_diameter,
                 m.hole_clearance, m.hole_to_hole, m.copper_edge_clearance, m.solder_mask_to_copper_clearance})
    value(static_cast<std::uint64_t>(v));
  value(static_cast<std::uint64_t>(obs.via_mask()));
  for (const auto& net : b.nets) {
    text(net.name); text(r.class_for(net.name).name);
  }
  for (const auto& fp : b.footprints) {
    text(fp.reference); text(fp.lib_id); value(fp.back); value(fp.locked);
    value(static_cast<std::uint64_t>(fp.clearance)); value(static_cast<std::uint64_t>(fp.mask_margin));
  }
  for (const auto& pad : b.pads) {
    text(pad.number); value(static_cast<std::uint64_t>(pad.type)); value(static_cast<std::uint64_t>(pad.shape));
    for (Coord v : {pad.size_x, pad.size_y, pad.clearance, pad.mask_margin}) value(static_cast<std::uint64_t>(v));
  }
  value(m.allow_blind_buried_vias);
  for (const auto& c : r.classes) {
    text(c.name);
    for (Coord v : {c.clearance, c.track_width, c.via_diameter, c.via_drill}) value(static_cast<std::uint64_t>(v));
  }
  for (const auto& rule : r.custom) {
    text(rule.name); text(rule.condition); text(rule.layer); text(rule.severity); value(static_cast<std::uint64_t>(rule.origin));
    for (const auto& c : rule.constraints) {
      text(c.type);
      for (const auto v : {c.min, c.opt, c.max}) { value(v.has_value()); if (v) value(static_cast<std::uint64_t>(*v)); }
      for (const auto& s : c.items) text(s);
    }
  }
  auto shape = [&](const geom::Shape& s) {
    value(static_cast<std::uint64_t>(s.r)); value(s.closed);
    for (Point p : s.pts) { value(static_cast<std::uint64_t>(p.x)); value(static_cast<std::uint64_t>(p.y)); }
  };
  for (const auto& it : obs.copper().items) {
    value(static_cast<std::uint64_t>(it.net)); value(it.layers); value(static_cast<std::uint64_t>(it.kind));
    for (const auto& s : it.shapes) shape(s);
  }
  for (const auto& h : obs.copper().holes) shape(h.shape);
  for (const auto& e : obs.copper().edges) shape(e);
  for (const auto& zone : b.zones) {
    if (!zone.rule_area) continue;
    value(zone.copper); value(zone.keepout_tracks); value(zone.keepout_vias);
    for (const auto& poly : zone.outline) for (Point p : poly) {
      value(static_cast<std::uint64_t>(p.x)); value(static_cast<std::uint64_t>(p.y));
    }
  }
  return hash;
}
}

std::vector<PartEscape> analyse_escapes(const model::Board& b, const model::DesignRules& r, Obstacles& obs, const EscapeAnalysisOptions& o,
                                        const EscapeOptions& eo) {
  std::vector<PartEscape> out;
  const auto fingerprint = domain_hash(b, r, obs);
  const auto connectivity = drc::compute_connectivity(b, obs.copper(), obs.grid(), true);
  std::map<model::NetId, std::set<int>> net_roots;
  if (o.routing.soft_zones) {
    std::map<model::NetId, model::LayerMask> pad_layers;
    for (const auto& pad : b.pads) pad_layers[pad.net] |= pad.copper;
    for (std::size_t i = 0; i < obs.copper().items.size(); ++i) {
      const auto& it = obs.copper().items[i];
      if (it.kind != drc::ItemKind::Zone || it.footprint >= 0 || b.zones[z(it.index)].teardrop || it.net <= 0) continue;
      model::LayerMask allowed = 0;
      for (int l = 0; l < b.copper_count(); ++l)
        if (obs.rules().track_allowed(it.net, l)) allowed |= model::layer_bit(l);
      const auto pads = pad_layers[it.net] & allowed, planes = it.layers & allowed;
      const bool change = obs.rules().via_allowed(it.net) && (o.routing.allow_vias ||
          (o.routing.blind_vias && r.minimums.allow_blind_buried_vias && b.copper_count() > 2));
      if ((pads & planes) || (pads && planes && change)) net_roots[it.net].insert(connectivity.root[i]);
    }
  }
  std::vector<int> pad_items(b.pads.size(), -1);
  for (std::size_t i = 0; i < obs.copper().items.size(); ++i) {
    const auto& it = obs.copper().items[i];
    if (it.kind != drc::ItemKind::Pad) continue;
    pad_items[z(it.index)] = static_cast<int>(i);
    if (it.net > 0) net_roots[it.net].insert(connectivity.root[i]);
  }
  const int nl = b.copper_count();
  for (std::size_t fi = 0; fi < b.footprints.size(); ++fi) {
    const auto& fp = b.footprints[fi];
    std::vector<int> pads;
    for (int pi : fp.pads) {
      const auto& p = b.pads[z(pi)];
      if (p.copper != 0 && p.type != model::PadType::NpThruHole) pads.push_back(pi);
    }
    if (static_cast<int>(pads.size()) < eo.min_pads) continue;
    Coord pitch = LLONG_MAX;
    for (std::size_t i = 0; i < pads.size(); ++i)
      for (std::size_t j = i + 1; j < pads.size(); ++j) {
        const Point d = b.pads[z(pads[j])].pos - b.pads[z(pads[i])].pos;
        const Coord dist = static_cast<Coord>(std::llround(std::hypot(static_cast<double>(d.x), static_cast<double>(d.y))));
        if (dist > 0) pitch = std::min(pitch, dist);
      }
    if (pitch == LLONG_MAX || pitch > eo.max_pitch) continue;
    Coord x0 = LLONG_MAX, y0 = LLONG_MAX, x1 = LLONG_MIN, y1 = LLONG_MIN;
    for (int pi : pads) {
      const Point q = b.pads[z(pi)].pos;
      x0 = std::min(x0, q.x), x1 = std::max(x1, q.x), y0 = std::min(y0, q.y), y1 = std::max(y1, q.y);
    }
    PartEscape pe;
    pe.footprint = static_cast<int>(fi);
    pe.ref = fp.reference;
    pe.pitch = pitch;
    // Lattice over the package and its surroundings.
    const Coord L = analysis_pitch(r, o);
    const auto bounds = obs.bounds();
    const Coord bx0 = bounds.x0 / L * L, by0 = bounds.y0 / L * L;
    const Coord gx0 = bx0 + (x0 - o.window - bx0) / L * L, gy0 = by0 + (y0 - o.window - by0) / L * L;
    const int nx = static_cast<int>((x1 + o.window - gx0) / L) + 1, ny = static_cast<int>((y1 + o.window - gy0) / L) + 1;
    auto pt = [&](int ix, int iy) { return Point{gx0 + static_cast<Coord>(ix) * L, gy0 + static_cast<Coord>(iy) * L}; };
    auto outside = [&](Point p) { return p.x < x0 - o.margin || p.x > x1 + o.margin || p.y < y0 - o.margin || p.y > y1 + o.margin; };
    const std::size_t cells = static_cast<std::size_t>(nx) * static_cast<std::size_t>(ny);
    auto key = [&](int l, int ix, int iy) { return static_cast<std::size_t>(l) * cells + z(iy) * z(nx) + z(ix); };
    auto decode = [&](std::size_t k) {
      return std::tuple{static_cast<int>(k / cells), static_cast<int>((k % cells) % z(nx)), static_cast<int>((k % cells) / z(nx))};
    };
    for (int pi : pads) {
      const auto& p = b.pads[z(pi)];
      if (p.net <= 0) continue;
      PinEscape pin;
      pin.pad = pi;
      pin.domain = "pitch=" + std::to_string(L) + "nm; zones=" + (o.routing.soft_zones ? "refillable" : "hard") +
          "; keep_vias_off_pads=" + std::to_string(o.routing.keep_vias_off_pads) + "nm; blind_vias=" +
          std::to_string(o.routing.blind_vias) + "; domain=" + std::to_string(fingerprint);
      if (net_roots[p.net].size() <= 1) {
        pin.status = "satisfied";
        pin.reason = "existing copper connects every required terminal of this net in the routing domain";
        pe.results.push_back(std::move(pin));
        continue;
      }
      ++pe.pins;
      const auto& nc = r.class_for(b.nets[z(p.net)].name);
      const Coord width = std::max(nc.track_width, r.minimums.track_width), hw = width / 2;
      const auto via = neck_down_via(r, nc);
      const Coord dia = via.diameter, drill = via.drill;
      pin.domain = "pitch=" + std::to_string(L) + "nm; widths=" + std::to_string(access_width_floor(r)) + ".." +
          std::to_string(width) + "nm; via=" + std::to_string(dia) + "/" + std::to_string(drill) +
          "nm; zones=" + (o.routing.soft_zones ? "refillable" : "hard") +
          "; geometry portals radius=4000000nm; corners=dot<=0.99; midpoints=8/axis; edges=16/octant/1500000nm; keep_vias_off_pads=" +
          std::to_string(o.routing.keep_vias_off_pads) + "nm; blind_vias=" + std::to_string(o.routing.blind_vias) +
          "; domain=" + std::to_string(fingerprint);
      std::vector<std::int8_t> tc(static_cast<std::size_t>(nl) * cells, -1), vc(cells, -1);
      auto tok = [&](int l, int ix, int iy) {
        auto& code = tc[key(l, ix, iy)];
        if (code < 0) {
          const bool allowed = obs.rules().track_allowed(p.net, l);
          if (!allowed) code = 0;
          else if (o.reference || obs.needs_exact_routing()) code = obs.disk_state(pt(ix, iy), l, hw, p.net, 0, true) != 2;
          else {
            const auto c = obs.fixed_code(pt(ix, iy), l, hw, 0, p.net);
            code = c == Obstacles::kFree || c == p.net;
            // Compact codes intentionally omit diff-pair relief. Recheck conservative negatives so the
            // accelerated and uncached domains contain exactly the same legal nodes.
            if (!code) code = obs.disk_state(pt(ix, iy), l, hw, p.net, 0, true) != 2;
          }
        }
        return code != 0;
      };
      auto vok = [&](int ix, int iy) {
        auto& code = vc[z(iy) * z(nx) + z(ix)];
        if (code < 0) code = o.routing.allow_vias && obs.rules().via_allowed(p.net) &&
            obs.via_state(pt(ix, iy), dia, drill, p.net, 0, true) != 2;
        return code != 0;
      };
      // Start: lattice points inside the pad on its layers (pad copper is the pin's own net).
      std::vector<std::size_t> parent(static_cast<std::size_t>(nl) * cells, SIZE_MAX);
      std::deque<std::tuple<int, int, int>> q;
      const auto [hx, hy] = half_extents(p);
      const int item = pad_items[z(pi)];
      auto on_pad = [&](Point point) {
        if (item < 0) return false;
        for (const auto& shape : obs.copper().items[z(item)].shapes)
          if (geom::closer_than_disk(shape, point, 0, 1)) return true;
        return false;
      };
      for (int l = 0; l < nl; ++l) {
        if (!(p.copper & (model::LayerMask{1} << l)) || !obs.rules().track_allowed(p.net, l)) continue;
        const int ix0 = static_cast<int>((p.pos.x - hx - gx0) / L), ix1 = static_cast<int>((p.pos.x + hx - gx0) / L) + 1;
        const int iy0 = static_cast<int>((p.pos.y - hy - gy0) / L), iy1 = static_cast<int>((p.pos.y + hy - gy0) / L) + 1;
        bool any = false;
        for (int iy = std::max(0, iy0); iy <= std::min(ny - 1, iy1); ++iy)
          for (int ix = std::max(0, ix0); ix <= std::min(nx - 1, ix1); ++ix)
            if (on_pad(pt(ix, iy)) && tok(l, ix, iy) &&
                obs.segment_state(p.pos, pt(ix, iy), l, width, p.net, true) != 2) {
              const auto k = key(l, ix, iy);
              if (parent[k] == SIZE_MAX) {
                parent[k] = k;
                q.emplace_back(l, ix, iy);
                any = true;
              }
            }
        if (!any) {
          const int ix = std::clamp(static_cast<int>((p.pos.x - gx0 + L / 2) / L), 0, nx - 1), iy = std::clamp(static_cast<int>((p.pos.y - gy0 + L / 2) / L), 0, ny - 1);
          if (tok(l, ix, iy) && obs.segment_state(p.pos, pt(ix, iy), l, width, p.net, true) != 2) {
            parent[key(l, ix, iy)] = key(l, ix, iy);
            q.emplace_back(l, ix, iy);
          }
        }
      }
      bool escaped = false, budget = false;
      long work = 0;
      std::size_t goal = SIZE_MAX;
      static constexpr int kDx[8] = {1, -1, 0, 0, 1, 1, -1, -1}, kDy[8] = {0, 0, 1, -1, 1, -1, 1, -1};
      while (!q.empty() && !escaped) {
        if (o.work_budget > 0 && work++ >= o.work_budget) { budget = true; break; }
        const auto [l, ix, iy] = q.front();
        q.pop_front();
        if (outside(pt(ix, iy))) {
          escaped = true;
          goal = key(l, ix, iy);
          break;
        }
        for (int d = 0; d < 8; ++d) {
          const int jx = ix + kDx[d], jy = iy + kDy[d];
          if (jx < 0 || jy < 0 || jx >= nx || jy >= ny) continue;
          const std::size_t k = (static_cast<std::size_t>(l) * static_cast<std::size_t>(ny) + static_cast<std::size_t>(jy)) * static_cast<std::size_t>(nx) + static_cast<std::size_t>(jx);
          if (parent[k] != SIZE_MAX || !tok(l, jx, jy) ||
              obs.segment_state(pt(ix, iy), pt(jx, jy), l, width, p.net, true) == 2) continue;
          parent[k] = key(l, ix, iy);
          q.emplace_back(l, jx, jy);
        }
        if (nl > 1 && vok(ix, iy)) {
          for (int l2 = 0; l2 < nl; ++l2) {
            const std::size_t k = (static_cast<std::size_t>(l2) * static_cast<std::size_t>(ny) + static_cast<std::size_t>(iy)) * static_cast<std::size_t>(nx) + static_cast<std::size_t>(ix);
            if (parent[k] != SIZE_MAX || !tok(l2, ix, iy)) continue;
            parent[k] = key(l, ix, iy);
            q.emplace_back(l2, ix, iy);
          }
        }
      }
      if (escaped) {
        pin.status = "witness";
        pin.witness.layer = std::get<0>(decode(goal));
        const auto [gl, gx, gy] = decode(goal);
        (void)gl;
        pin.witness.end = pt(gx, gy);
        auto k = goal;
        while (parent[k] != k) {
          const auto [l, ix, iy] = decode(k);
          const auto [pl, px, py] = decode(parent[k]);
          if (l == pl) pin.witness.steps.push_back({pt(px, py), pt(ix, iy), l, width});
          else pin.witness.vias.push_back({pt(ix, iy), dia, drill, 0, nl - 1, model::ViaType::Through, p.net, false, sexpr::kNoNode});
          k = parent[k];
        }
        const auto [sl, sx, sy] = decode(k);
        if (!(p.pos == pt(sx, sy))) pin.witness.steps.push_back({p.pos, pt(sx, sy), sl, width});
        std::reverse(pin.witness.steps.begin(), pin.witness.steps.end());
        std::reverse(pin.witness.vias.begin(), pin.witness.vias.end());
      } else {
        AccessSearchOptions ao;
        ao.origin = {bx0, by0};
        ao.pitch = L;
        ao.routing = o.routing;
        ao.reference = o.reference;
        ao.max_paths = 1;
        ao.work_budget = o.work_budget;
        ao.target = [&](Point point, int, const std::function<bool()>&) {
          return outside(point) ? AccessGoal::Endpoint : AccessGoal::None;
        };
        auto access = generate_access_paths(b, r, obs, pi, ao);
        if (!access.paths.empty()) {
          pin.status = "witness";
          pin.witness = std::move(access.paths.front());
        } else {
          pin.status = budget || !access.exhausted || !obs.rules().warnings().empty() || !r.warnings.empty() ? "unknown" : "exhausted";
          pin.reason = budget || !access.exhausted ? "work budget exhausted in configured access graph" :
              pin.status == "unknown" ? "unsupported rule context: see routing-domain warnings" : "no escape in configured router search domain";
          if (pin.status == "exhausted") pe.dead.push_back({pi, pin.reason});
        }
      }
      if (pin.status == "witness") ++pe.escapable;
      pe.results.push_back(std::move(pin));
    }
    if (pe.results.empty()) continue;
    out.push_back(std::move(pe));
  }
  return out;
}

}  // namespace tmk::route
