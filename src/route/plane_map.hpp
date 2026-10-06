// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once

#include <cstdint>
#include <vector>

#include "model/board.hpp"

namespace tmk::route {
// KiCad zone priorities select copper ownership; ties retain document order (D61). This map is a search
// preference, not connectivity or a legality raster: only a KiCad refill can establish the final plane.
class PlaneMap {
 public:
  static model::NetId reference(const model::Board& b, geom::Point p, int layer);
  // Convert once per search, not in the cell loop; truncation keeps the surcharge at most F times the cost.
  static std::int64_t scaled_penalty(std::int64_t geometric_cost, double factor);
  // False means the uint16 zone table cannot represent the board; the map remains empty.
  bool build(const model::Board& b, geom::Point origin, Coord pitch, int nx, int ny);
  model::NetId net_at(int layer, int x, int y) const {
    if (owner_.empty()) return 0;
    const auto index = (static_cast<std::size_t>(layer) * static_cast<std::size_t>(ny_) + static_cast<std::size_t>(y)) *
                           static_cast<std::size_t>(nx_) + static_cast<std::size_t>(x);
    return nets_[owner_[index]];
  }
  std::int64_t cost(int layer, int x, int y, model::NetId net, std::int64_t penalty) const {
    const auto plane = net_at(layer, x, y);
    return plane != 0 && plane != net ? penalty : 0;
  }

 private:
  int nx_ = 0, ny_ = 0;
  std::vector<std::uint16_t> owner_;
  std::vector<model::NetId> nets_;
};
}  // namespace tmk::route
