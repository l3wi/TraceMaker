// SPDX-License-Identifier: GPL-3.0-or-later
#include <catch2/catch_test_macros.hpp>

#include <bit>
#include <random>

#include "route/pair_exit_guidance.hpp"

using namespace tmk;
using geom::Point;
using route::PairExitGuide;

namespace {
template <class Allow>
double original_cost(const std::vector<PairExitGuide>& guides, Point p, Point q, int layer,
                     model::NetId net, Coord width, Coord clearance, Coord pitch, Allow&& allow) {
  double cost = 0;
  for (const auto& guide : guides) {
    if (!(guide.layers & model::layer_bit(layer)) || guide.net == net || !allow(guide.net)) continue;
    if (geom::seg_seg_closer(p, q, guide.a, guide.e, (width + guide.width) / 2 + clearance))
      cost += 8.0 * static_cast<double>(pitch);
  }
  return cost;
}

void identical(double a, double b) {
  CHECK(std::bit_cast<std::uint64_t>(a) == std::bit_cast<std::uint64_t>(b));
}
}  // namespace

TEST_CASE("pair exit guidance preserves randomized exact costs and memo keys", "[diffpair][guidance]") {
  std::mt19937_64 rng(160);
  const auto coord = [&] { return static_cast<Coord>(rng() % 2001) - 1000; };
  std::vector<PairExitGuide> guides;
  for (int i = 0; i < 220; ++i) {
    const Point a{coord(), coord()};
    Point e{coord(), coord()};
    if (i % 9 == 0) e = a;
    guides.push_back({a, e, model::layer_bit(i % 4) | model::layer_bit((i + 1) % 4),
                      static_cast<model::NetId>(i % 8), static_cast<Coord>(rng() % 101)});
    if (i % 7 == 0) guides.push_back(guides.back());
  }
  route::PairExitGuidance index;
  index.rebuild(guides);
  for (int i = 0; i < 6000; ++i) {
    const Point p{coord(), coord()};
    const Point q = i % 5 == 0 ? p : Point{coord(), coord()};
    const int layer = static_cast<int>(rng() % 5);
    const auto net = static_cast<model::NetId>(rng() % 8);
    const Coord width = static_cast<Coord>(rng() % 101);
    const Coord clearance = static_cast<Coord>(rng() % 31);
    const Coord pitch = i % 13 == 0 ? 9'007'199'254'740'993LL : 150'001;
    const auto allow = [net](model::NetId other) { return other != (net ^ 1); };
    for (const bool reversed : {false, true}) {
      const Point a = reversed ? q : p, b = reversed ? p : q;
      const double expected = original_cost(guides, a, b, layer, net, width, clearance, pitch, allow);
      identical(index.cost(a, b, layer, net, width, clearance, pitch, false, allow), expected);
      identical(index.cost(a, b, layer, net, width, clearance, pitch, true, allow), expected);
      identical(index.cost(a, b, layer, net, width, clearance, pitch, true, allow), expected);  // memo hit
    }
  }
}

TEST_CASE("pair exit guidance retains duplicate hits, odd radii and strict boundaries", "[diffpair][guidance]") {
  std::vector<PairExitGuide> guides(101, {{-10, 0}, {10, 0}, model::layer_bit(0), 3, 5});
  guides.push_back({{10, 0}, {-10, 0}, model::layer_bit(1), 3, 5});
  guides.push_back({{0, 0}, {0, 0}, model::layer_bit(0), 1, 5});  // self
  guides.push_back({{0, 0}, {0, 0}, model::layer_bit(0), 2, 5});  // partner
  route::PairExitGuidance index;
  index.rebuild(guides);
  const auto allow = [](model::NetId net) { return net != 2; };
  for (const Coord y : {-5, -4, -3, 0, 3, 4, 5}) {
    for (const int layer : {0, 1, 2}) {
      for (const Coord width : {0, 1, 2, 3, 4, 5}) {
        for (const Coord clearance : {0, 1, 2}) {
          const Coord pitch = 9'007'199'254'740'993LL;
          const Point p{-20, y}, q{20, y};
          identical(index.cost(p, q, layer, 1, width, clearance, pitch, true, allow),
                    original_cost(guides, p, q, layer, 1, width, clearance, pitch, allow));
        }
      }
    }
  }
  CHECK(index.cost({-20, 3}, {20, 3}, 0, 1, 3, 0, 1, true, allow) == 101 * 8.0);
  CHECK(index.cost({-20, 4}, {20, 4}, 0, 1, 3, 0, 1, true, allow) == 0);
}

TEST_CASE("pair exit guidance memo collisions evict safely and rebuild invalidates eligibility", "[diffpair][guidance]") {
  std::vector<PairExitGuide> guides{{{-10, 0}, {10, 0}, model::layer_bit(0), 3, 5}};
  route::PairExitGuidance index;
  index.rebuild(guides);
  const auto allow = [](model::NetId) { return true; };
  // More distinct keys than direct-map slots guarantees collisions. Alternate
  // zero/nonzero costs and replay in reverse so stale hits cannot pass silently.
  for (const bool backwards : {false, true}) {
    for (int i = 0; i < 12'000; ++i) {
      const int n = backwards ? 11'999 - i : i;
      const Point p{0, n % 2 == 0 ? 0 : 10};
      const Coord pitch = static_cast<Coord>(n) + 1;
      identical(index.cost(p, p, 0, 1, 3, 0, pitch, true, allow),
                original_cost(guides, p, p, 0, 1, 3, 0, pitch, allow));
    }
  }
  CHECK(index.cost({0, 0}, {0, 0}, 0, 1, 3, 0, 1, true, allow) == 8);
  index.rebuild(guides);  // eligibility belongs to a new immutable generation
  const auto deny = [](model::NetId) { return false; };
  CHECK(index.cost({0, 0}, {0, 0}, 0, 1, 3, 0, 1, true, deny) == 0);
  guides[0].a.y = guides[0].e.y = 100;
  index.rebuild(guides);
  CHECK(index.cost({0, 0}, {0, 0}, 0, 1, 3, 0, 1, true, allow) == 0);
  index.rebuild({});
  CHECK(index.cost({0, 0}, {0, 0}, 0, 1, 3, 0, 1, true, allow) == 0);
  index.rebuild({{{0, 0}, {0, 0}, model::layer_bit(0), 3, 5}}, false);
  CHECK(index.cost({0, 0}, {0, 0}, 0, 1, 3, 0, 1, true, allow) == 8);
}
