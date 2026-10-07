// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once

#include <memory>
#include <span>
#include <string>
#include <string_view>
#include <vector>

#include "drc/copper.hpp"

namespace tmk::drc {

// Immutable prepared polygon set, with nested rings interpreted by even/odd depth.
// Area intersection regions are deflated by DRC epsilon; courtyards by KiCad's 5 um.
struct RuleRegion {
  std::vector<std::vector<geom::Point>> rings;
  geom::Box box;
  model::LayerMask layers = 0;
  int zone = -1;
  int footprint = -1;
  int side = 0;  // physical side: 1 front, 2 back
};

// Snapshot of the board geometry. Reconstruct after footprint moves/flips or zone edits.
// KiCad 10.0.3 pcbexpr_functions.cpp and footprint.cpp define the predicate semantics.
class RuleGeometry {
 public:
  explicit RuleGeometry(const model::Board& board);
  ~RuleGeometry();
  bool area(const CopperItem& item, std::string_view selector, bool enclosed) const;
  bool courtyard(const CopperItem& item, std::string_view selector, int side = 0) const;
  // Compile-time selector binding; candidate intersection queries do not allocate.
  std::vector<std::size_t> area_regions(std::string_view selector) const;
  std::vector<std::size_t> courtyard_regions(std::string_view selector, int side = 0) const;
  bool area(const CopperItem& item, std::span<const std::size_t> regions, bool enclosed) const;
  bool courtyard(const CopperItem& item, std::span<const std::size_t> regions) const;
  // Independent preparation and linear scans, without cached-region bbox pruning.
  bool area_reference(const CopperItem& item, std::string_view selector, bool enclosed) const;
  bool courtyard_reference(const CopperItem& item, std::string_view selector, int side = 0) const;
  const std::vector<std::string>& warnings() const { return warnings_; }
  std::vector<std::string> validate_area(std::string_view selector) const;
  std::vector<std::string> validate_courtyard(std::string_view selector) const;
  std::span<const RuleRegion> areas() const { return areas_; }
  std::span<const RuleRegion> courtyards() const { return courtyards_; }

 private:
  struct PolygonCache;
  std::unique_ptr<PolygonCache> polygons_;
  const model::Board& board_;
  std::vector<RuleRegion> areas_, outlines_, courtyards_;
  std::vector<std::vector<RuleRegion>> fills_;
  std::vector<std::string> warnings_;
  std::vector<std::string> area_errors_, courtyard_errors_;
  bool area_impl(const CopperItem&, std::string_view, bool, bool) const;
  bool courtyard_impl(const CopperItem&, std::string_view, int, bool) const;
  bool area_hit(const CopperItem&, std::size_t, bool, bool) const;
  bool courtyard_hit(const CopperItem&, std::size_t, bool) const;
};

}  // namespace tmk::drc
