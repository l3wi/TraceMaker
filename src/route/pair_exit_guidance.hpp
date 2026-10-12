// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once

#include <algorithm>
#include <array>
#include <numeric>
#include <utility>
#include <vector>

#include "core/rng.hpp"
#include "geom/shape.hpp"
#include "model/board.hpp"

namespace tmk::route {

struct PairExitGuide {
  geom::Point a, e;
  model::LayerMask layers;
  model::NetId net;
  Coord width;
};

// Median-split AABB tree (Bentley, 1975), used only as a conservative broad phase.
// Guides and net eligibility are immutable between pair passes. The caller's allow
// predicate must remain unchanged until rebuild(), which invalidates the memo.
class PairExitGuidance {
 public:
  void rebuild(std::vector<PairExitGuide> guides, bool indexed = true) {
    guides_ = std::move(guides);
    nodes_.clear();
    for (auto& entry : cache_) entry.valid = false;
    order_.resize(indexed ? guides_.size() : 0);
    std::iota(order_.begin(), order_.end(), std::size_t{0});
    nodes_.reserve(2 * order_.size());
    if (!order_.empty()) build(0, order_.size());
    indexed_ = indexed;
  }

  template <class Allow>
  double cost(geom::Point p, geom::Point q, int layer, model::NetId net,
              Coord width, Coord clearance, Coord pitch, bool indexed, Allow&& allow) const {
    indexed = indexed && indexed_;
    // Retain directed endpoints: even the reference predicate's input order is unchanged.
    const std::array<Coord, 9> key{p.x, p.y, q.x, q.y, layer, net, width, clearance, pitch};
    CachedCost* memo = nullptr;
    if (indexed) {
      std::uint64_t hash = 0;
      for (const Coord value : key) hash = splitmix64(hash ^ static_cast<std::uint64_t>(value));
      memo = &cache_[static_cast<std::size_t>(hash) & (cache_.size() - 1)];
      if (memo->valid && memo->key == key) return memo->cost;
    }
    std::size_t hits = 0;
    const auto test = [&](std::size_t id) {
      const auto& guide = guides_[id];
      if (!(guide.layers & model::layer_bit(layer)) || guide.net == net || !allow(guide.net)) return;
      if (geom::seg_seg_closer(p, q, guide.a, guide.e, (width + guide.width) / 2 + clearance)) ++hits;
    };
    if (!indexed) {
      for (std::size_t i = 0; i < guides_.size(); ++i) test(i);
    } else if (!nodes_.empty()) {
      geom::Box probe;
      probe.add(p);
      probe.add(q);
      probe = probe.inflated(width / 2 + width % 2 + clearance);
      const auto visit = [&](auto&& self, std::size_t id) -> void {
        const auto& node = nodes_[id];
        if (!node.box.intersects(probe)) return;
        if (node.leaf) test(node.guide);
        else { self(self, node.left); self(self, node.right); }
      };
      visit(visit, 0);
    }
    // Every hit adds the SAME term. Repeat the original additions, rather than
    // multiplying the hit count: even pitches beyond double's exact-integer range
    // then have the reference rounding sequence, independent of traversal order.
    double result = 0;
    const double penalty = 8.0 * static_cast<double>(pitch);
    for (std::size_t i = 0; i < hits; ++i) result += penalty;
    if (memo) *memo = {key, result, true};
    return result;
  }

 private:
  struct Node {
    geom::Box box;
    std::size_t left = 0, right = 0, guide = 0;
    bool leaf = false;
  };
  struct CachedCost { std::array<Coord, 9> key{}; double cost = 0; bool valid = false; };
  std::vector<PairExitGuide> guides_;
  std::vector<Node> nodes_;
  std::vector<std::size_t> order_;
  bool indexed_ = false;
  mutable std::array<CachedCost, 4096> cache_{};  // collisions only evict; no per-query allocation

  std::size_t build(std::size_t first, std::size_t last) {
    const std::size_t id = nodes_.size();
    nodes_.push_back({});
    geom::Box box;
    for (std::size_t i = first; i < last; ++i) {
      const auto& guide = guides_[order_[i]];
      geom::Box bounds;
      bounds.add(guide.a);
      bounds.add(guide.e);
      box.add(bounds.inflated(guide.width / 2 + guide.width % 2));
    }
    nodes_[id].box = box;
    if (last - first == 1) {
      nodes_[id].guide = order_[first];
      nodes_[id].leaf = true;
    } else {
      const bool x = box.x1 - box.x0 >= box.y1 - box.y0;
      const std::size_t middle = first + (last - first) / 2;
      std::nth_element(order_.begin() + static_cast<std::ptrdiff_t>(first),
                       order_.begin() + static_cast<std::ptrdiff_t>(middle),
                       order_.begin() + static_cast<std::ptrdiff_t>(last), [&](std::size_t a, std::size_t b) {
        const auto& ga = guides_[a];
        const auto& gb = guides_[b];
        const Coord ca = x ? std::midpoint(ga.a.x, ga.e.x) : std::midpoint(ga.a.y, ga.e.y);
        const Coord cb = x ? std::midpoint(gb.a.x, gb.e.x) : std::midpoint(gb.a.y, gb.e.y);
        return ca != cb ? ca < cb : a < b;
      });
      const std::size_t left = build(first, middle);
      const std::size_t right = build(middle, last);
      nodes_[id].left = left;
      nodes_[id].right = right;
    }
    return id;
  }
};

}  // namespace tmk::route
