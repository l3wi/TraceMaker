// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
// Which copper layers may carry new tracks, and what a track costs on each (doc 05 §29/§41).
#include <cstdint>
#include <string>
#include <vector>

#include "model/board.hpp"

namespace tmk::route {

struct ProtectedPlane {
  int layer = -1;
  model::NetId net = 0;
  double coverage = 0;  // union of same-net zone outlines clipped to Edge.Cuts / board area
};

struct LayerLimits {
  model::LayerMask no_track_layers = 0;     // copper layers that get no new tracks
  std::vector<std::int32_t> layer_cost_pm;  // per copper index: track cost per mille (1000 = normal); empty = all normal
  std::vector<ProtectedPlane> protected_planes;  // stack order; strongest net per layer, ties by net ID
  std::vector<std::string> warnings;             // unreadable outline: no automatic protection
};

inline constexpr double kMaxLayerCost = 1000.0;
inline constexpr double kPlaneCoverage = 0.6;

// `no_tracks_on`: copper layer names. `layer_costs`: "NAME=FACTOR" entries with 1 <= FACTOR <= kMaxLayerCost.
// Names are the board's canonical names ("In1.Cu"), the names written in the file, or the user's layer names.
// Throws std::invalid_argument on an unknown layer, a malformed entry, a factor out of range, or when no layer
// would be left for tracks: a limit the router cannot apply as written must stop the run, not be dropped.
LayerLimits layer_limits(const model::Board& b, const std::vector<std::string>& no_tracks_on,
                         const std::vector<std::string>& layer_costs, bool protect_planes = false);

}  // namespace tmk::route
