// SPDX-License-Identifier: GPL-3.0-or-later
#include "route/layer_limits.hpp"

#include <algorithm>
#include <charconv>
#include <cmath>
#include <map>
#include <stdexcept>
#include "geom/shape.hpp"

namespace tmk::route {

namespace {

std::string copper_names(const model::Board& b) {
  std::string s;
  for (int i = 0; i < b.copper_count(); ++i) s += (i ? ", " : "") + b.copper_name(i);
  return s;
}

int copper_layer(const model::Board& b, const std::string& name, const char* option) {
  int idx = b.copper_index(name);
  for (int i = 0; idx < 0 && i < b.copper_count(); ++i) {
    const auto& l = b.layers[static_cast<std::size_t>(b.copper[static_cast<std::size_t>(i)])];
    if (!l.user_name.empty() && l.user_name == name) idx = i;
  }
  if (idx < 0) throw std::invalid_argument(std::string(option) + ": \"" + name + "\" is not a copper layer of this board (" + copper_names(b) + ")");
  return idx;
}

using geom::Point;
using Rings = std::vector<std::vector<Point>>;

// Edge.Cuts uses the same endpoint-chain assembly as courtyard contours (drc/rule_geometry.cpp).
// Never replace an open/ambiguous outline by its bounding box: that could invent a plane.
bool same_endpoint(Point a, Point b) {
  // KiCad's outline chaining tolerates 0.01 mm (rounded arc endpoints in real boards).
  constexpr Coord tolerance = 10'000;
  const auto dx = static_cast<geom::i128>(a.x) - b.x, dy = static_cast<geom::i128>(a.y) - b.y;
  return dx * dx + dy * dy <= static_cast<geom::i128>(tolerance) * tolerance;
}

Rings board_rings(const model::Board& b) {
  Rings rings, chains;
  for (const auto& g : b.graphics) {
    if (g.layer != "Edge.Cuts") continue;
    switch (g.kind) {
      case model::Graphic::Kind::Line: chains.push_back({g.a, g.b}); break;
      case model::Graphic::Kind::Arc: chains.push_back(geom::arc_points(g.a, g.c, g.b, 1'000)); break;
      case model::Graphic::Kind::Circle:
        rings.push_back(geom::circle_points(g.a, geom::kiround(std::hypot(
            static_cast<double>(g.b.x - g.a.x), static_cast<double>(g.b.y - g.a.y))), 1'000));
        break;
      case model::Graphic::Kind::Rect:
        rings.push_back(g.pts.empty() ? std::vector<Point>{g.a, {g.b.x, g.a.y}, g.b, {g.a.x, g.b.y}} : g.pts);
        break;
      case model::Graphic::Kind::Poly: rings.push_back(g.pts); break;
      case model::Graphic::Kind::Curve: return {};
    }
  }
  std::vector<bool> used(chains.size());
  for (std::size_t start = 0; start < chains.size(); ++start) {
    if (used[start]) continue;
    auto ring = std::move(chains[start]);
    used[start] = true;
    if (ring.size() < 2) return {};
    while (!same_endpoint(ring.front(), ring.back())) {
      std::size_t next = chains.size();
      bool reverse = false;
      for (std::size_t i = 0; i < chains.size(); ++i) {
        if (used[i] || chains[i].empty()) continue;
        if (same_endpoint(chains[i].front(), ring.back()) || same_endpoint(chains[i].back(), ring.back())) {
          if (next != chains.size()) return {};
          next = i;
          reverse = same_endpoint(chains[i].back(), ring.back());
        }
      }
      if (next == chains.size()) return {};
      used[next] = true;
      if (reverse) std::reverse(chains[next].begin(), chains[next].end());
      ring.insert(ring.end(), chains[next].begin() + 1, chains[next].end());
    }
    ring.back() = ring.front();
    rings.push_back(std::move(ring));
  }
  for (auto& ring : rings) {
    ring.erase(std::unique(ring.begin(), ring.end()), ring.end());
    if (ring.size() > 1 && ring.front() == ring.back()) ring.pop_back();
    if (ring.size() < 3) return {};
  }
  return rings;
}

struct AreaEdge {
  Point a, b;
  std::size_t group;  // 0: board (even/odd); each zone has its own even/odd group
};

// Vertical decomposition / trapezoidal integration (Preparata & Shamos, Computational Geometry, 1985):
// membership and edge order are constant between vertices and proper edge intersections, so integrating
// the clipped union's length at the slab midpoint is exact for straight edges. No fill cache, raster, or
// overlapping-zone double count. Integer predicates find crossings; long double is only for area reporting.
long double clipped_area(const Rings& board, const std::vector<const model::Zone*>& zones) {
  std::vector<AreaEdge> edges;
  std::vector<long double> cuts;
  auto append = [&](const Rings& rings, std::size_t group) {
    for (const auto& ring : rings) {
      if (ring.size() < 3) continue;
      for (std::size_t i = 0; i < ring.size(); ++i) {
        const Point a = ring[i], c = ring[(i + 1) % ring.size()];
        cuts.push_back(static_cast<long double>(a.x));
        if (a != c) edges.push_back({a, c, group});
      }
    }
  };
  append(board, 0);
  for (std::size_t i = 0; i < zones.size(); ++i) append(zones[i]->outline, i + 1);
  for (std::size_t i = 0; i < edges.size(); ++i) {
    const auto& a = edges[i];
    for (std::size_t j = i + 1; j < edges.size(); ++j) {
      const auto& c = edges[j];
      if (std::max(a.a.x, a.b.x) <= std::min(c.a.x, c.b.x) ||
          std::max(c.a.x, c.b.x) <= std::min(a.a.x, a.b.x)) continue;
      const auto p = geom::orient(a.a, a.b, c.a), q = geom::orient(a.a, a.b, c.b);
      const auto r = geom::orient(c.a, c.b, a.a), s = geom::orient(c.a, c.b, a.b);
      if (!((p < 0 && q > 0) || (p > 0 && q < 0)) ||
          !((r < 0 && s > 0) || (r > 0 && s < 0))) continue;
      const long double t = static_cast<long double>(r) / static_cast<long double>(r - s);
      cuts.push_back(static_cast<long double>(a.a.x) + t * static_cast<long double>(a.b.x - a.a.x));
    }
  }
  std::sort(cuts.begin(), cuts.end());
  cuts.erase(std::unique(cuts.begin(), cuts.end()), cuts.end());
  std::vector<std::pair<long double, std::size_t>> hits;
  hits.reserve(edges.size());
  std::vector<bool> inside(zones.size() + 1);
  long double area = 0;
  for (std::size_t i = 1; i < cuts.size(); ++i) {
    const long double x = (cuts[i - 1] + cuts[i]) / 2;
    hits.clear();
    for (const auto& e : edges) {
      const long double ax = static_cast<long double>(e.a.x), bx = static_cast<long double>(e.b.x);
      if (x <= std::min(ax, bx) || x >= std::max(ax, bx)) continue;
      const long double y = static_cast<long double>(e.a.y) +
          (x - ax) * static_cast<long double>(e.b.y - e.a.y) / (bx - ax);
      hits.emplace_back(y, e.group);
    }
    std::sort(hits.begin(), hits.end());
    std::fill(inside.begin(), inside.end(), false);
    std::size_t occupied = 0;
    long double length = 0;
    for (std::size_t j = 0; j < hits.size(); ++j) {
      const auto [y, group] = hits[j];
      if (group != 0) {
        if (inside[group]) --occupied;
        else ++occupied;
      }
      inside[group] = !inside[group];
      if (j + 1 < hits.size() && inside[0] && (zones.empty() || occupied != 0))
        length += hits[j + 1].first - y;
    }
    area += length * (cuts[i] - cuts[i - 1]);
  }
  return area;
}

void protect_inner_planes(const model::Board& b, LayerLimits& out) {
  if (b.copper_count() <= 2) return;
  const auto rings = board_rings(b);
  const auto area = clipped_area(rings, {});
  if (area <= 0) {
    out.warnings.push_back("--protect-planes: no readable closed Edge.Cuts area; no planes detected");
    return;
  }
  for (int layer = 1; layer + 1 < b.copper_count(); ++layer) {
    std::map<model::NetId, std::vector<const model::Zone*>> nets;
    for (const auto& z : b.zones)
      if (!z.rule_area && !z.teardrop && z.net > 0 && (z.copper & model::layer_bit(layer)))
        nets[z.net].push_back(&z);
    ProtectedPlane best{layer, 0, 0};
    for (const auto& [net, zones] : nets) {
      const double coverage = static_cast<double>(clipped_area(rings, zones) / area);
      if (coverage >= kPlaneCoverage && coverage > best.coverage) best = {layer, net, coverage};
    }
    if (best.net == 0) continue;
    out.no_track_layers |= model::layer_bit(layer);
    out.protected_planes.push_back(best);
  }
}

}  // namespace

LayerLimits layer_limits(const model::Board& b, const std::vector<std::string>& no_tracks_on,
                         const std::vector<std::string>& layer_costs, bool protect_planes) {
  LayerLimits out;
  for (const auto& name : no_tracks_on) out.no_track_layers |= model::layer_bit(copper_layer(b, name, "--no-tracks-on"));
  if (protect_planes) protect_inner_planes(b, out);
  const model::LayerMask all = b.copper_count() >= 64 ? ~model::LayerMask{0} : model::layer_bit(b.copper_count()) - 1;
  if (out.no_track_layers != 0 && (out.no_track_layers & all) == all)
    throw std::invalid_argument("--no-tracks-on / --protect-planes: no copper layer is left for tracks");
  for (const auto& entry : layer_costs) {
    // The layer name may itself contain '=' only in theory; the factor is what follows the last one.
    const auto eq = entry.rfind('=');
    if (eq == std::string::npos || eq == 0 || eq + 1 == entry.size()) throw std::invalid_argument("--layer-cost: \"" + entry + "\" is not LAYER=FACTOR");
    const int idx = copper_layer(b, entry.substr(0, eq), "--layer-cost");
    double f = 0;
    const char* first = entry.data() + eq + 1;
    const char* last = entry.data() + entry.size();
    const auto [end, ec] = std::from_chars(first, last, f);
    // Below 1 a track would cost less than its length, which the search's distance bounds do not allow for.
    if (ec != std::errc() || end != last || !(f >= 1.0) || f > kMaxLayerCost)
      throw std::invalid_argument("--layer-cost: the factor in \"" + entry + "\" must be a number from 1 to " + std::to_string(static_cast<int>(kMaxLayerCost)));
    if (out.layer_cost_pm.empty()) out.layer_cost_pm.assign(static_cast<std::size_t>(b.copper_count()), 1000);
    out.layer_cost_pm[static_cast<std::size_t>(idx)] = static_cast<std::int32_t>(std::llround(f * 1000.0));
  }
  return out;
}

}  // namespace tmk::route
