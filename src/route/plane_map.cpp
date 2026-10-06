// SPDX-License-Identifier: GPL-3.0-or-later
#include "route/plane_map.hpp"

#include <algorithm>
#include <limits>

#include "geom/shape.hpp"

namespace tmk::route {
namespace {

bool conductive(const model::Zone& z) {
  return !z.rule_area && z.net != 0 && z.copper != 0 && !z.outline.empty();
}

bool contains(const model::Zone& z, geom::Point p) {
  if (!geom::point_in_polygon(p, z.outline.front())) return false;
  for (std::size_t i = 1; i < z.outline.size(); ++i) {
    if (geom::point_in_polygon(p, z.outline[i])) return false;
  }
  return true;
}

geom::i128 floor_div(geom::i128 n, geom::i128 d) {
  const auto q = n / d;
  return q - (n % d < 0 ? 1 : 0);
}

// Keep crossings rational: floating-point rounding at a lattice point would change boundary ownership.
// The integer part also keeps sorting products bounded by two edge lengths, not three coordinates.
struct Crossing {
  Coord whole, remainder, denominator;
};

Crossing crossing(geom::Point a, geom::Point b, Coord y) {
  if (a.y > b.y) std::swap(a, b);
  const Coord dy = b.y - a.y;
  const geom::i128 numerator = static_cast<geom::i128>(a.x) * dy + static_cast<geom::i128>(y - a.y) * (b.x - a.x);
  const auto whole = floor_div(numerator, dy);
  return {static_cast<Coord>(whole), static_cast<Coord>(numerator - whole * dy), dy};
}

bool crossing_less(const Crossing& a, const Crossing& b) {
  if (a.whole != b.whole) return a.whole < b.whole;
  return static_cast<geom::i128>(a.remainder) * b.denominator < static_cast<geom::i128>(b.remainder) * a.denominator;
}

struct Boundary {
  geom::i128 first, last;
  bool hole;
};

// Scan conversion uses even-odd crossings of the outer ring and holes (Foley et al., Computer Graphics:
// Principles and Practice, polygon scan conversion). Outer boundaries are inside, hole boundaries outside,
// matching the closed-polygon convention in geom::point_in_polygon and the reference zone containment.
void raster_row(const model::Zone& z, geom::Point origin, Coord pitch, int y, std::vector<std::uint8_t>& row,
                std::vector<Crossing>& crossings, std::vector<Boundary>& boundaries) {
  std::fill(row.begin(), row.end(), 0);
  crossings.clear();
  boundaries.clear();
  const Coord py = origin.y + static_cast<Coord>(y) * pitch;
  for (std::size_t ring = 0; ring < z.outline.size(); ++ring) {
    const auto& poly = z.outline[ring];
    for (std::size_t i = 0, j = poly.size() - 1; i < poly.size(); j = i++) {
      const auto a = poly[j], b = poly[i];
      if (a.y == b.y) {
        if (a.y == py) {
          const auto left = static_cast<geom::i128>(std::min(a.x, b.x)) - origin.x;
          const auto right = static_cast<geom::i128>(std::max(a.x, b.x)) - origin.x;
          boundaries.push_back({-floor_div(-left, pitch), floor_div(right, pitch), ring != 0});
        }
        continue;
      }
      if (py < std::min(a.y, b.y) || py > std::max(a.y, b.y)) continue;
      const auto c = crossing(a, b, py);
      // One end is open so vertices do not toggle parity twice; both ends still count as boundary.
      if ((a.y > py) != (b.y > py)) crossings.push_back(c);
      const auto offset = static_cast<geom::i128>(c.whole) - origin.x;
      if (c.remainder == 0 && offset % pitch == 0) {
        boundaries.push_back({offset / pitch, offset / pitch, ring != 0});
      }
    }
  }
  std::sort(crossings.begin(), crossings.end(), crossing_less);
  auto fill = [&](geom::i128 first, geom::i128 last, std::uint8_t value) {
    first = std::max<geom::i128>(first, 0);
    last = std::min<geom::i128>(last, static_cast<geom::i128>(row.size()) - 1);
    if (first <= last) {
      std::fill(row.begin() + static_cast<std::ptrdiff_t>(first), row.begin() + static_cast<std::ptrdiff_t>(last + 1), value);
    }
  };
  for (std::size_t i = 0; i + 1 < crossings.size(); i += 2) {
    const auto left = static_cast<geom::i128>(crossings[i].whole) - origin.x;
    const auto right = static_cast<geom::i128>(crossings[i + 1].whole) - origin.x;
    const auto first = floor_div(left, pitch) + 1;
    auto last = floor_div(right, pitch);
    if (crossings[i + 1].remainder == 0 && right % pitch == 0) --last;
    fill(first, last, 1);
  }
  // Rings were visited outer-first, so a hole boundary wins even where it touches the outer outline.
  for (const auto& boundary : boundaries) {
    fill(boundary.first, boundary.last, boundary.hole ? 0 : 1);
  }
}

}  // namespace

model::NetId PlaneMap::reference(const model::Board& b, geom::Point p, int layer) {
  model::NetId net = 0;
  int priority = 0;
  for (const auto& z : b.zones) {
    if (!conductive(z) || !(z.copper & model::layer_bit(layer)) || !contains(z, p)) continue;
    if (net == 0 || z.priority > priority) {
      net = z.net;
      priority = z.priority;
    }
  }
  return net;
}

bool PlaneMap::build(const model::Board& b, geom::Point origin, Coord pitch, int nx, int ny) {
  nx_ = nx;
  ny_ = ny;
  owner_.clear();
  nets_.clear();
  std::vector<const model::Zone*> zones;
  for (const auto& z : b.zones) {
    if (conductive(z)) zones.push_back(&z);
  }
  if (zones.size() > std::numeric_limits<std::uint16_t>::max()) return false;
  if (zones.empty()) return true;
  std::stable_sort(zones.begin(), zones.end(), [](const auto* a, const auto* c) { return a->priority > c->priority; });
  nets_.reserve(zones.size() + 1);
  nets_.push_back(0);
  for (const auto* z : zones) {
    nets_.push_back(z->net);
  }
  owner_.assign(static_cast<std::size_t>(b.copper_count()) * static_cast<std::size_t>(nx) * static_cast<std::size_t>(ny), 0);
  std::vector<std::uint8_t> row(static_cast<std::size_t>(nx));
  std::vector<Crossing> crossings;
  std::vector<Boundary> boundaries;
  for (std::size_t zi = 0; zi < zones.size(); ++zi) {
    const auto& z = *zones[zi];
    geom::Box box;
    for (const auto& p : z.outline.front()) {
      box.add(p);
    }
    if (box.empty()) continue;
    const int x0 = static_cast<int>(std::clamp<geom::i128>(floor_div(static_cast<geom::i128>(box.x0) - origin.x, pitch), 0, nx - 1));
    const int x1 = static_cast<int>(std::clamp<geom::i128>(floor_div(static_cast<geom::i128>(box.x1) - origin.x, pitch) + 1, 0, nx - 1));
    const int y0 = static_cast<int>(std::clamp<geom::i128>(floor_div(static_cast<geom::i128>(box.y0) - origin.y, pitch), 0, ny - 1));
    const int y1 = static_cast<int>(std::clamp<geom::i128>(floor_div(static_cast<geom::i128>(box.y1) - origin.y, pitch) + 1, 0, ny - 1));
    const auto zone_index = static_cast<std::uint16_t>(zi + 1);
    for (int y = y0; y <= y1; ++y) {
      raster_row(z, origin, pitch, y, row, crossings, boundaries);
      for (int l = 0; l < b.copper_count(); ++l) {
        if (!(z.copper & model::layer_bit(l))) continue;
        const auto offset = (static_cast<std::size_t>(l) * static_cast<std::size_t>(ny) + static_cast<std::size_t>(y)) * static_cast<std::size_t>(nx);
        for (int x = x0; x <= x1; ++x) {
          auto& owner = owner_[offset + static_cast<std::size_t>(x)];
          if (row[static_cast<std::size_t>(x)] && owner == 0) owner = zone_index;
        }
      }
    }
  }
  return true;
}

}  // namespace tmk::route
