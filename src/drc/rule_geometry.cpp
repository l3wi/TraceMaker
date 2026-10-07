// SPDX-License-Identifier: GPL-3.0-or-later
#include "drc/rule_geometry.hpp"

#include <algorithm>
#include <cmath>
#include <limits>

#include <clipper2/clipper.h>

#include "model/rules.hpp"

namespace tmk::drc {
// Keep Clipper's representation private and cached: an enclosure query must not
// allocate/reconvert unchanged target contours before the required Boolean operation.
struct RuleGeometry::PolygonCache {
  std::vector<Clipper2Lib::Paths64> outlines;
};

namespace {
using geom::Point;
using geom::Shape;
using Clipper2Lib::Path64;
using Clipper2Lib::Paths64;
constexpr Coord kAreaEpsilon = 500;
constexpr Coord kCourtyardError = 5'000;
constexpr Coord kChainingEpsilon = 20'000;

Path64 path(const std::vector<Point>& points) {
  Path64 result;
  result.reserve(points.size());
  for (Point p : points) result.emplace_back(p.x, p.y);
  return result;
}
Paths64 paths(const RuleRegion& region) {
  Paths64 result;
  result.reserve(region.rings.size());
  for (const auto& ring : region.rings) result.push_back(path(ring));
  return result;
}
RuleRegion region_from_paths(const Paths64& polygons) {
  RuleRegion result;
  for (const auto& polygon : polygons) {
    auto& ring = result.rings.emplace_back();
    ring.reserve(polygon.size());
    for (const auto& p : polygon) {
      ring.push_back({p.x, p.y});
      result.box.add(ring.back());
    }
  }
  return result;
}

// Validate before Boolean normalization: a malformed contour must not silently turn
// into a plausible union. Integer predicates are shared with the DRC geometry kernel.
bool valid_rings(std::vector<std::vector<Point>>& rings) {
  for (auto& ring : rings) {
    ring.erase(std::unique(ring.begin(), ring.end()), ring.end());
    if (ring.size() > 1 && ring.front() == ring.back()) ring.pop_back();
    if (ring.size() < 3) return false;
    geom::i128 area = 0;
    for (std::size_t i = 0; i < ring.size(); ++i) {
      const Point a = ring[i], b = ring[(i + 1) % ring.size()];
      area += static_cast<geom::i128>(a.x) * b.y - static_cast<geom::i128>(a.y) * b.x;
      for (std::size_t j = i + 1; j < ring.size(); ++j) {
        if (j == i + 1 || (i == 0 && j + 1 == ring.size())) continue;
        if (geom::segments_intersect(a, b, ring[j], ring[(j + 1) % ring.size()])) return false;
      }
    }
    if (area == 0) return false;
  }
  for (std::size_t i = 0; i < rings.size(); ++i)
    for (std::size_t j = i + 1; j < rings.size(); ++j)
      for (std::size_t a = 0; a < rings[i].size(); ++a)
        for (std::size_t b = 0; b < rings[j].size(); ++b)
          if (geom::segments_intersect(rings[i][a], rings[i][(a + 1) % rings[i].size()],
                                       rings[j][b], rings[j][(b + 1) % rings[j].size()])) return false;
  return !rings.empty();
}
RuleRegion prepare(std::vector<std::vector<Point>> rings, Coord deflate, double miter_limit) {
  RuleRegion raw;
  raw.rings = std::move(rings);
  // EvenOdd handles arbitrary input winding, nested holes and disjoint outer rings.
  auto normalized = Clipper2Lib::Union(paths(raw), Clipper2Lib::FillRule::EvenOdd);
  if (deflate > 0)
    normalized = Clipper2Lib::InflatePaths(normalized, -static_cast<double>(deflate),
                                         Clipper2Lib::JoinType::Miter, Clipper2Lib::EndType::Polygon,
                                         miter_limit, static_cast<double>(kCourtyardError));
  return region_from_paths(normalized);
}
bool on_edge(Point p, Point a, Point b) {
  return geom::orient(a, b, p) == 0 && p.x >= std::min(a.x, b.x) && p.x <= std::max(a.x, b.x) &&
         p.y >= std::min(a.y, b.y) && p.y <= std::max(a.y, b.y);
}
bool contains(const RuleRegion& region, Point p) {
  bool inside = false;
  for (const auto& ring : region.rings) {
    for (std::size_t i = 0; i < ring.size(); ++i)
      if (on_edge(p, ring[i], ring[(i + 1) % ring.size()])) return true;
    if (geom::point_in_polygon(p, ring)) inside = !inside;
  }
  return inside;
}
template<class F> bool edges(const Shape& shape, F&& f) {
  if (shape.pts.size() == 1) return f(shape.pts.front(), shape.pts.front());
  for (std::size_t i = 1; i < shape.pts.size(); ++i)
    if (f(shape.pts[i - 1], shape.pts[i])) return true;
  return shape.closed && !shape.pts.empty() && f(shape.pts.back(), shape.pts.front());
}
template<class F> bool boundaries(const RuleRegion& region, F&& f) {
  for (const auto& ring : region.rings)
    for (std::size_t i = 0; i < ring.size(); ++i)
      if (f(ring[i], ring[(i + 1) % ring.size()])) return true;
  return false;
}
// Inclusive distance for endpoint chaining and Bezier control-hull tolerance.
bool point_segment_within(Point p, Point a, Point b, Coord radius) {
  const geom::i128 dx = b.x - a.x, dy = b.y - a.y;
  const geom::i128 px = p.x - a.x, py = p.y - a.y;
  const geom::i128 length = dx * dx + dy * dy, projection = px * dx + py * dy;
  const geom::i128 r2 = static_cast<geom::i128>(radius) * radius;
  if (length == 0 || projection <= 0) return px * px + py * py <= r2;
  if (projection >= length) {
    const geom::i128 ex = p.x - b.x, ey = p.y - b.y;
    return ex * ex + ey * ey <= r2;
  }
  const geom::i128 cross = px * dy - py * dx;
  return cross * cross <= r2 * length;
}
bool segments_within(Point a, Point b, Point c, Point d, Coord radius) {
  // SHAPE_POLY_SET::Collide uses zero distance or strictly less than radius.
  return geom::segments_intersect(a, b, c, d) ||
         (radius > 0 && geom::seg_seg_closer(a, b, c, d, radius));
}
bool collides(const RuleRegion& region, const Shape& shape) {
  if (shape.pts.empty() || region.rings.empty()) return false;
  if (contains(region, shape.pts.front())) return true;
  if (shape.closed)
    for (const auto& ring : region.rings)
      if (geom::point_in_polygon(ring.front(), shape.pts)) return true;
  return edges(shape, [&](Point a, Point b) {
    return boundaries(region, [&](Point c, Point d) { return segments_within(a, b, c, d, shape.r); });
  });
}
bool regions_collide(const RuleRegion& a, const RuleRegion& b) {
  for (const auto& ring : a.rings) if (contains(b, ring.front())) return true;
  for (const auto& ring : b.rings) if (contains(a, ring.front())) return true;
  return boundaries(a, [&](Point p, Point q) {
    return boundaries(b, [&](Point u, Point v) { return geom::segments_intersect(p, q, u, v); });
  });
}
bool enclosed(const RuleRegion& region, const Paths64& clipping_paths, const Shape& shape) {
  if (shape.pts.empty() || region.rings.empty()) return false;
  for (Point p : shape.pts) if (!contains(region, p)) return false;
  // Boolean subtraction checks every edge/interior, not just vertices or bbox corners.
  Paths64 subject;
  if (shape.pts.size() > 1) subject.push_back(path(shape.pts));
  if (shape.closed) {
    if (!Clipper2Lib::Difference(subject, clipping_paths, Clipper2Lib::FillRule::EvenOdd).empty()) return false;
  } else if (shape.pts.size() > 1) {
    Clipper2Lib::Clipper64 clipper;
    clipper.AddOpenSubject(subject);
    clipper.AddClip(clipping_paths);
    Paths64 closed_result, open_result;
    clipper.Execute(Clipper2Lib::ClipType::Difference, Clipper2Lib::FillRule::EvenOdd, closed_result, open_result);
    if (!open_result.empty()) return false;
  }
  // Rounded cores stay enclosed iff their entire core is enclosed and the
  // distance from every core edge to every boundary is at least the radius.
  return shape.r <= 0 || !edges(shape, [&](Point a, Point b) {
    return boundaries(region, [&](Point c, Point d) { return geom::seg_seg_closer(a, b, c, d, shape.r); });
  });
}

bool is_uuid(std::string_view selector) {
  if (selector.size() != 36) return false;
  for (std::size_t i = 0; i < selector.size(); ++i) {
    if (i == 8 || i == 13 || i == 18 || i == 23) { if (selector[i] != '-') return false; }
    else if (!((selector[i] >= '0' && selector[i] <= '9') || (selector[i] >= 'a' && selector[i] <= 'f') ||
               (selector[i] >= 'A' && selector[i] <= 'F'))) return false;
  }
  return true;
}
bool zone_matches(const model::Zone& zone, std::string_view selector) {
  if (selector.empty() || selector == "A" || selector == "B") return false;  // unary A is excluded self; B absent
  if (!is_uuid(selector)) return model::wildcard_match(selector, zone.name);
  if (selector.size() != zone.uuid.size()) return false;
  auto lower_hex = [](char ch) { return ch >= 'A' && ch <= 'F' ? static_cast<char>(ch + ('a' - 'A')) : ch; };
  for (std::size_t i = 0; i < selector.size(); ++i)
    if (lower_hex(selector[i]) != lower_hex(zone.uuid[i])) return false;
  return true;
}
bool footprint_matches(const model::Footprint& fp, std::string_view selector) {
  if (selector.empty() || selector.front() == '$' || selector == "A" || selector == "B") return false;
  return model::wildcard_match(selector, fp.reference) ||
         (selector.find(':') != std::string_view::npos && model::wildcard_match(selector, fp.lib_id));
}
bool near(Point a, Point b) { return point_segment_within(a, b, b, kChainingEpsilon); }
Point midpoint(Point a, Point b) { return {a.x + (b.x - a.x) / 2, a.y + (b.y - a.y) / 2}; }

// de Casteljau subdivision (1959): a Bezier is contained by its control hull.
// Stop only when that hull lies within the courtyard polygonization tolerance.
void flatten_curve(Point a, Point b, Point c, Point d, std::vector<Point>& points) {
  if (point_segment_within(b, a, d, kCourtyardError) && point_segment_within(c, a, d, kCourtyardError)) {
    points.push_back(d);
    return;
  }
  const Point ab = midpoint(a, b), bc = midpoint(b, c), cd = midpoint(c, d);
  const Point abc = midpoint(ab, bc), bcd = midpoint(bc, cd), centre = midpoint(abc, bcd);
  flatten_curve(a, ab, abc, centre, points);
  flatten_curve(centre, bcd, cd, d, points);
}

std::vector<Point> rounded_rectangle(const model::Graphic& graphic) {
  auto corners = graphic.pts.empty() ? std::vector<Point>{graphic.a, {graphic.b.x, graphic.a.y}, graphic.b,
                                                         {graphic.a.x, graphic.b.y}} : graphic.pts;
  if (corners.size() != 4 || graphic.corner_radius <= 0) return corners;
  double radius = static_cast<double>(graphic.corner_radius);
  for (std::size_t i = 0; i < corners.size(); ++i) {
    const Point delta = corners[(i + 1) % corners.size()] - corners[i];
    radius = std::min(radius, std::hypot(static_cast<double>(delta.x), static_cast<double>(delta.y)) / 2.0);
  }
  if (radius <= 0) return {};
  std::vector<Point> result;
  for (std::size_t i = 0; i < corners.size(); ++i) {
    const Point corner = corners[i];
    auto towards = [&](Point other) {
      const Point delta = other - corner;
      const double length = std::hypot(static_cast<double>(delta.x), static_cast<double>(delta.y));
      return Point{geom::kiround(static_cast<double>(delta.x) * radius / length),
                   geom::kiround(static_cast<double>(delta.y) * radius / length)};
    };
    const Point before = towards(corners[(i + 3) % corners.size()]);
    const Point after = towards(corners[(i + 1) % corners.size()]);
    const Point centre = corner + before + after;
    const Point radial = corner - centre;
    const Point middle = centre + Point{geom::kiround(static_cast<double>(radial.x) * std::sqrt(0.5)),
                                        geom::kiround(static_cast<double>(radial.y) * std::sqrt(0.5))};
    const auto arc = geom::arc_points(corner + before, middle, corner + after, kCourtyardError);
    result.insert(result.end(), arc.begin(), arc.end());
  }
  return result;
}

bool courtyard_rings(const model::Board& board, const model::Footprint& fp, int side,
                     std::vector<std::vector<Point>>& rings) {
  std::vector<std::vector<Point>> chains;
  for (int index : fp.graphics) {
    if (index < 0 || static_cast<std::size_t>(index) >= board.graphics.size()) return false;
    const auto& graphic = board.graphics[static_cast<std::size_t>(index)];
    if (graphic.layer != (side == 1 ? "F.CrtYd" : "B.CrtYd")) continue;
    switch (graphic.kind) {
      case model::Graphic::Kind::Line: chains.push_back({graphic.a, graphic.b}); break;
      case model::Graphic::Kind::Arc:
        chains.push_back(geom::arc_points(graphic.a, graphic.c, graphic.b, kCourtyardError)); break;
      case model::Graphic::Kind::Circle: {
        const Coord radius = geom::kiround(std::hypot(static_cast<double>(graphic.b.x - graphic.a.x),
                                                     static_cast<double>(graphic.b.y - graphic.a.y)));
        rings.push_back(geom::circle_points(graphic.a, radius, kCourtyardError));
        break;
      }
      case model::Graphic::Kind::Rect:
        rings.push_back(rounded_rectangle(graphic));
        break;
      case model::Graphic::Kind::Poly: rings.push_back(graphic.pts); break;
      case model::Graphic::Kind::Curve: {
        if (graphic.pts.size() != 4) return false;
        std::vector<Point> points{graphic.pts.front()};
        flatten_curve(graphic.pts[0], graphic.pts[1], graphic.pts[2], graphic.pts[3], points);
        chains.push_back(std::move(points));
        break;
      }
    }
  }
  std::vector<bool> used(chains.size());
  for (std::size_t start = 0; start < chains.size(); ++start) {
    if (used[start]) continue;
    auto ring = chains[start];
    used[start] = true;
    while (!near(ring.front(), ring.back())) {
      std::size_t next = chains.size();
      bool reverse = false;
      geom::i128 best = std::numeric_limits<Coord>::max();
      for (std::size_t i = 0; i < chains.size(); ++i) {
        if (used[i]) continue;
        for (int end = 0; end < 2; ++end) {
          const Point p = end == 0 ? chains[i].front() : chains[i].back();
          const geom::i128 dx = p.x - ring.back().x, dy = p.y - ring.back().y;
          const geom::i128 distance = dx * dx + dy * dy;
          if (near(p, ring.back()) && distance < best) {
            best = distance;
            next = i;
            reverse = end != 0;
          }
        }
      }
      if (next == chains.size()) return false;
      used[next] = true;
      if (reverse) std::reverse(chains[next].begin(), chains[next].end());
      // Snap the next segment endpoint to the previous endpoint as KiCad's chaining does.
      ring.insert(ring.end(), chains[next].begin() + 1, chains[next].end());
    }
    ring.back() = ring.front();
    rings.push_back(std::move(ring));
  }
  return rings.empty() || valid_rings(rings);
}
}  // namespace

RuleGeometry::RuleGeometry(const model::Board& board) : polygons_(std::make_unique<PolygonCache>()), board_(board) {
  area_errors_.resize(board.zones.size());
  courtyard_errors_.resize(board.footprints.size() * 2);
  fills_.resize(board.zones.size());
  for (std::size_t i = 0; i < board.zones.size(); ++i) {
    const auto& zone = board.zones[i];
    auto rings = zone.outline;
    RuleRegion outline, intersection;
    if (!valid_rings(rings)) {
      area_errors_[i] = "area '" + zone.name + "' has malformed or unavailable outline geometry";
      warnings_.push_back(area_errors_[i]);
    } else {
      outline = prepare(rings, 0, 2.0);
      // shape_poly_set.cpp ALLOW_ACUTE_CORNERS uses miter limit 10.
      intersection = prepare(std::move(rings), kAreaEpsilon, 10.0);
    }
    outline.zone = intersection.zone = static_cast<int>(i);
    outline.footprint = intersection.footprint = zone.footprint;
    outline.layers = intersection.layers = zone.copper;
    outlines_.push_back(std::move(outline));
    polygons_->outlines.push_back(paths(outlines_.back()));
    areas_.push_back(std::move(intersection));
    for (const auto& [layer, points] : zone.fills) {
      if (layer < 0 || layer >= 64 || points.size() < 3) continue;
      auto fill = prepare({points}, 0, 2.0);
      fill.layers = model::layer_bit(layer);
      fills_[i].push_back(std::move(fill));
    }
  }
  for (std::size_t i = 0; i < board.footprints.size(); ++i) {
    const auto& fp = board.footprints[i];
    for (int side = 1; side <= 2; ++side) {
      std::vector<std::vector<Point>> rings;
      RuleRegion region;
      if (!courtyard_rings(board, fp, side, rings)) {
        const std::size_t slot = i * 2 + static_cast<std::size_t>(side - 1);
        courtyard_errors_[slot] = "footprint '" + fp.reference + "' has malformed or unavailable " +
                                 (side == 1 ? "front" : "back") + " courtyard geometry";
        warnings_.push_back(courtyard_errors_[slot]);
      } else if (!rings.empty()) {
        // footprint.cpp BuildCourtyardCaches: 5 um deflation, acute-corner chamfer.
        region = prepare(std::move(rings), kCourtyardError, 2.0);
      }
      region.footprint = static_cast<int>(i);
      region.side = side;
      courtyards_.push_back(std::move(region));
    }
  }
}

RuleGeometry::~RuleGeometry() = default;

bool RuleGeometry::area_hit(const CopperItem& item, std::size_t i, bool enclosure, bool prune) const {
  if (i >= areas_.size() || !(item.layers & areas_[i].layers) ||
      (item.kind == ItemKind::Zone && item.index == static_cast<int>(i))) return false;
  const RuleRegion& region = enclosure ? outlines_[i] : areas_[i];
  const bool zone_outline = item.kind == ItemKind::Zone && item.index >= 0 &&
                            static_cast<std::size_t>(item.index) < outlines_.size();
  const auto& box = zone_outline ? outlines_[static_cast<std::size_t>(item.index)].box : item.box;
  if (prune && !region.box.intersects(box)) return false;
  if (zone_outline) {
    const std::size_t source_index = static_cast<std::size_t>(item.index);
    if (enclosure) {
      const auto& source = outlines_[source_index];
      return !source.rings.empty() && !region.rings.empty() &&
             Clipper2Lib::Difference(polygons_->outlines[source_index], polygons_->outlines[i],
                                    Clipper2Lib::FillRule::EvenOdd).empty();
    }
    // KiCad's zone rtree queries all filled polygons on the shared test layers.
    for (const auto& fill : fills_[source_index])
      if ((fill.layers & item.layers & region.layers) && (!prune || region.box.intersects(fill.box)) &&
          regions_collide(region, fill)) return true;
    return false;
  }
  if (enclosure) {
    if (item.shapes.empty()) return false;
    for (const auto& shape : item.shapes) if (!enclosed(region, polygons_->outlines[i], shape)) return false;
    return true;
  }
  for (const auto& shape : item.shapes) if (collides(region, shape)) return true;
  return false;
}
bool RuleGeometry::courtyard_hit(const CopperItem& item, std::size_t i, bool prune) const {
  if (i >= courtyards_.size()) return false;
  const auto& region = courtyards_[i];
  const bool zone_outline = item.kind == ItemKind::Zone && item.index >= 0 &&
                            static_cast<std::size_t>(item.index) < outlines_.size();
  const auto& box = zone_outline ? outlines_[static_cast<std::size_t>(item.index)].box : item.box;
  if (prune && !region.box.intersects(box)) return false;
  // Courtyard functions intentionally do not require a common copper layer.
  if (zone_outline) return regions_collide(region, outlines_[static_cast<std::size_t>(item.index)]);
  for (const auto& shape : item.shapes) if (collides(region, shape)) return true;
  return false;
}
bool RuleGeometry::area_impl(const CopperItem& item, std::string_view selector, bool enclosure, bool prune) const {
  for (std::size_t i = 0; i < board_.zones.size(); ++i)
    if (zone_matches(board_.zones[i], selector) && area_hit(item, i, enclosure, prune)) return true;
  return false;
}
bool RuleGeometry::courtyard_impl(const CopperItem& item, std::string_view selector, int side, bool prune) const {
  if (side < 0 || side > 2) return false;
  for (std::size_t i = 0; i < courtyards_.size(); ++i) {
    const auto& region = courtyards_[i];
    const auto& fp = board_.footprints[static_cast<std::size_t>(region.footprint)];
    const int physical_side = side == 0 ? 0 : (fp.back ? 3 - side : side);
    if (footprint_matches(fp, selector) && (physical_side == 0 || region.side == physical_side) &&
        courtyard_hit(item, i, prune)) return true;
  }
  return false;
}
std::vector<std::size_t> RuleGeometry::area_regions(std::string_view selector) const {
  std::vector<std::size_t> result;
  for (std::size_t i = 0; i < board_.zones.size(); ++i)
    if (zone_matches(board_.zones[i], selector)) result.push_back(i);
  return result;
}
std::vector<std::size_t> RuleGeometry::courtyard_regions(std::string_view selector, int side) const {
  std::vector<std::size_t> result;
  if (side < 0 || side > 2) return result;
  for (std::size_t i = 0; i < courtyards_.size(); ++i) {
    const auto& region = courtyards_[i];
    const auto& fp = board_.footprints[static_cast<std::size_t>(region.footprint)];
    const int physical_side = side == 0 ? 0 : (fp.back ? 3 - side : side);
    if (footprint_matches(fp, selector) && (physical_side == 0 || region.side == physical_side)) result.push_back(i);
  }
  return result;
}
bool RuleGeometry::area(const CopperItem& item, std::span<const std::size_t> regions, bool enclosure) const {
  for (std::size_t i : regions) if (area_hit(item, i, enclosure, true)) return true;
  return false;
}
bool RuleGeometry::courtyard(const CopperItem& item, std::span<const std::size_t> regions) const {
  for (std::size_t i : regions) if (courtyard_hit(item, i, true)) return true;
  return false;
}
bool RuleGeometry::area(const CopperItem& item, std::string_view selector, bool enclosure) const {
  return area_impl(item, selector, enclosure, true);
}
bool RuleGeometry::courtyard(const CopperItem& item, std::string_view selector, int side) const {
  return courtyard_impl(item, selector, side, true);
}
bool RuleGeometry::area_reference(const CopperItem& item, std::string_view selector, bool enclosure) const {
  const RuleGeometry reference(board_);
  return reference.area_impl(item, selector, enclosure, false);
}
bool RuleGeometry::courtyard_reference(const CopperItem& item, std::string_view selector, int side) const {
  const RuleGeometry reference(board_);
  return reference.courtyard_impl(item, selector, side, false);
}
std::vector<std::string> RuleGeometry::validate_area(std::string_view selector) const {
  std::vector<std::string> result;
  if (selector.empty()) result.push_back("missing area selector");
  if (selector == "B") result.push_back("area selector B is unavailable in a unary disallow condition");
  for (std::size_t i = 0; i < board_.zones.size(); ++i)
    if (zone_matches(board_.zones[i], selector) && !area_errors_[i].empty()) result.push_back(area_errors_[i]);
  return result;
}
std::vector<std::string> RuleGeometry::validate_courtyard(std::string_view selector) const {
  std::vector<std::string> result;
  if (selector.empty()) result.push_back("missing footprint selector");
  if (!selector.empty() && selector.front() == '$') result.push_back("component-class footprint selector metadata is unavailable");
  if (selector == "A" || selector == "B") result.push_back("footprint-object selector is unavailable for unary copper disallow conditions");
  for (std::size_t i = 0; i < board_.footprints.size(); ++i) {
    const auto& fp = board_.footprints[i];
    if (!footprint_matches(fp, selector)) continue;
    for (std::size_t side = 0; side < 2; ++side)
      if (!courtyard_errors_[i * 2 + side].empty()) result.push_back(courtyard_errors_[i * 2 + side]);
    if (courtyards_[i * 2].rings.empty() && courtyards_[i * 2 + 1].rings.empty() &&
        courtyard_errors_[i * 2].empty() && courtyard_errors_[i * 2 + 1].empty())
      result.push_back("footprint '" + fp.reference + "' has no courtyard geometry");
  }
  return result;
}
}  // namespace tmk::drc
