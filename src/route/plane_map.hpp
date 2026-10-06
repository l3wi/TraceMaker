// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once

#include <algorithm>
#include <vector>

#include "geom/shape.hpp"
#include "model/board.hpp"

namespace tmk::route {
// KiCad zone priorities select copper ownership; ties retain document order (D61). This map is a search
// preference, not connectivity or a legality raster: only a KiCad refill can establish the final plane.
class PlaneMap {
 public:
  static model::NetId reference(const model::Board& b, geom::Point p, int layer) {
    model::NetId net = 0;
    int priority = 0;
    for (const auto& z : b.zones) {
      if (!conductive(z) || !(z.copper & model::layer_bit(layer)) || !contains(z, p)) continue;
      if (net == 0 || z.priority > priority) { net = z.net; priority = z.priority; }
    }
    return net;
  }
  void build(const model::Board& b, geom::Point origin, Coord pitch, int nx, int ny) {
    nx_ = nx; ny_ = ny;
    std::vector<const model::Zone*> zones;
    for (const auto& z : b.zones) if (conductive(z)) zones.push_back(&z);
    if (zones.empty()) return;
    std::stable_sort(zones.begin(), zones.end(), [](const auto* a, const auto* c) { return a->priority > c->priority; });
    net_.assign(static_cast<std::size_t>(b.copper_count()) * static_cast<std::size_t>(nx) * static_cast<std::size_t>(ny), 0);
    for (const auto* z : zones) {
      geom::Box box;
      for (const auto& p : z->outline.front()) box.add(p);
      const int x0 = static_cast<int>(std::clamp<Coord>((box.x0 - origin.x) / pitch, 0, nx - 1));
      const int x1 = static_cast<int>(std::clamp<Coord>((box.x1 - origin.x) / pitch + 1, 0, nx - 1));
      const int y0 = static_cast<int>(std::clamp<Coord>((box.y0 - origin.y) / pitch, 0, ny - 1));
      const int y1 = static_cast<int>(std::clamp<Coord>((box.y1 - origin.y) / pitch + 1, 0, ny - 1));
      for (int y = y0; y <= y1; ++y)
        for (int x = x0; x <= x1; ++x) {
          if (!contains(*z, {origin.x + x * pitch, origin.y + y * pitch})) continue;
          for (int l = 0; l < b.copper_count(); ++l) {
            if (!(z->copper & model::layer_bit(l))) continue;
            auto& n = net_[index(l, x, y)];
            if (n == 0) n = z->net;
          }
        }
    }
  }
  model::NetId net_at(int layer, int x, int y) const { return net_.empty() ? 0 : net_[index(layer, x, y)]; }
  std::int64_t cost(int layer, int x, int y, model::NetId net, std::int64_t penalty) const {
    const auto plane = net_at(layer, x, y);
    return plane != 0 && plane != net ? penalty : 0;
  }
 private:
  static bool conductive(const model::Zone& z) { return !z.rule_area && z.net != 0 && z.copper != 0 && !z.outline.empty(); }
  static bool contains(const model::Zone& z, geom::Point p) {
    if (!geom::point_in_polygon(p, z.outline.front())) return false;
    for (std::size_t i = 1; i < z.outline.size(); ++i) if (geom::point_in_polygon(p, z.outline[i])) return false;
    return true;
  }
  std::size_t index(int layer, int x, int y) const {
    return (static_cast<std::size_t>(layer) * static_cast<std::size_t>(ny_) + static_cast<std::size_t>(y)) * static_cast<std::size_t>(nx_) + static_cast<std::size_t>(x);
  }
  int nx_ = 0, ny_ = 0;
  std::vector<model::NetId> net_;
};
}  // namespace tmk::route
