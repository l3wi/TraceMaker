// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once

#include <algorithm>
#include <array>
#include <span>
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

// Median-split AABB tree (Bentley, 1975): guidance is immutable between pair passes.
// Broad phase only; every contributing guide still passes the original exact predicate.
// Net eligibility is fixed with the guides until rebuild; cache entries include all probe inputs.
class PairExitGuidance {
 public:
  template <class Poll>
  bool rebuild(std::span<const PairExitGuide> guides, Poll&& poll) {
    nodes_.clear();
    ready_ = false;
    for (auto& entry : cache_) entry.valid = false;
    order_.resize(guides.size());
    for (std::size_t i = 0; i < guides.size(); ++i) {
      if (!poll()) { order_.clear(); return false; }
      order_[i] = static_cast<int>(i);
    }
    nodes_.reserve(2 * guides.size());
    ready_ = order_.empty() || build(guides, 0, order_.size(), poll) >= 0;
    if (!ready_) { nodes_.clear(); order_.clear(); }
    return ready_;
  }

  template <class Allow, class Poll>
  double cost(std::span<const PairExitGuide> guides, geom::Point p, geom::Point q,
              int layer, model::NetId net, Coord width, Coord clearance, Coord pitch,
              bool indexed, Allow&& allow, Poll&& poll) const {
    indexed = indexed && ready_;  // a stopped rebuild must use the exact linear path, never a partial tree
    if (!poll()) return -1;
    geom::Point lo = p, hi = q;
    if (lo.x > hi.x || (lo.x == hi.x && lo.y > hi.y)) std::swap(lo, hi);
    const std::array<Coord, 9> key{lo.x, lo.y, hi.x, hi.y, layer, net, width, clearance, pitch};
    std::uint64_t hash = 0;
    for (const Coord value : key) hash = splitmix64(hash ^ static_cast<std::uint64_t>(value));
    auto& memo = cache_[static_cast<std::size_t>(hash) & (cache_.size() - 1)];
    if (indexed && memo.valid && memo.key == key) return memo.cost;
    long hits = 0;
    const auto test = [&](int id) {
      const auto& g = guides[static_cast<std::size_t>(id)];
      if (!(g.layers & model::layer_bit(layer)) || g.net == net || !allow(g.net)) return;
      if (geom::seg_seg_closer(p, q, g.a, g.e, (width + g.width) / 2 + clearance)) ++hits;
    };
    if (!indexed) {
      for (std::size_t i = 0; i < guides.size(); ++i) {
        if (!poll()) return -1;
        test(static_cast<int>(i));
      }
    } else if (!nodes_.empty()) {
      geom::Box probe;
      probe.add(p);
      probe.add(q);
      probe = probe.inflated((width + 1) / 2 + clearance);
      const auto visit = [&](auto&& self, int id) -> bool {
        if (!poll()) return false;
        const auto& n = nodes_[static_cast<std::size_t>(id)];
        if (!n.box.intersects(probe)) return true;
        if (n.guide >= 0) test(n.guide);
        else if (!self(self, n.left) || !self(self, n.right)) return false;
        return true;
      };
      if (!visit(visit, 0)) return -1;
    }
    // The old accumulation adds this same integral cost once per hit; visit order cannot affect it.
    const double cost = static_cast<double>(hits) * 8.0 * static_cast<double>(pitch);
    if (indexed) memo = {key, cost, true};
    return cost;
  }

 private:
  struct Node { geom::Box box; int left = -1, right = -1, guide = -1; };
  std::vector<Node> nodes_;
  std::vector<int> order_;
  struct CachedCost { std::array<Coord, 9> key{}; double cost = 0; bool valid = false; };
  bool ready_ = false;
  mutable std::array<CachedCost, 4096> cache_{};  // bounded, direct-mapped; a collision only evicts an entry

  template <class Poll>
  int build(std::span<const PairExitGuide> guides, std::size_t first, std::size_t last, Poll& poll) {
    const int id = static_cast<int>(nodes_.size());
    nodes_.push_back({});
    geom::Box box;
    for (std::size_t i = first; i < last; ++i) {
      if (!poll()) return -1;
      const auto& g = guides[static_cast<std::size_t>(order_[i])];
      geom::Box b;
      b.add(g.a);
      b.add(g.e);
      b = b.inflated((g.width + 1) / 2);
      box.add(b);
    }
    nodes_[static_cast<std::size_t>(id)].box = box;
    if (last - first == 1) nodes_[static_cast<std::size_t>(id)].guide = order_[first];
    else {
      const bool x = box.x1 - box.x0 >= box.y1 - box.y0;
      const auto middle = first + (last - first) / 2;
      std::nth_element(order_.begin() + static_cast<std::ptrdiff_t>(first), order_.begin() + static_cast<std::ptrdiff_t>(middle),
                       order_.begin() + static_cast<std::ptrdiff_t>(last), [&](int a, int b) {
        const auto& ga = guides[static_cast<std::size_t>(a)];
        const auto& gb = guides[static_cast<std::size_t>(b)];
        const Coord ca = x ? ga.a.x + ga.e.x : ga.a.y + ga.e.y;
        const Coord cb = x ? gb.a.x + gb.e.x : gb.a.y + gb.e.y;
        return ca != cb ? ca < cb : a < b;
      });
      const int left = build(guides, first, middle, poll);
      if (left < 0) return -1;
      const int right = build(guides, middle, last, poll);
      if (right < 0) return -1;
      nodes_[static_cast<std::size_t>(id)].left = left;
      nodes_[static_cast<std::size_t>(id)].right = right;
    }
    return id;
  }
};

}  // namespace tmk::route
