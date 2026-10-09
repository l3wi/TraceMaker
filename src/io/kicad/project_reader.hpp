// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
// Reads design rules from a KiCad project: <name>.kicad_pro (JSON) and <name>.kicad_dru (s-expressions).
#include <string>

#include "model/rules.hpp"

namespace tmk::io {

struct RuleSources {
  std::string project, dru;  // resolved absolute paths, even when implicit files are absent
};
RuleSources design_rule_sources(const std::string& board_path, const std::string& project_path = {});

// Explicit projects must exist. Without an override, missing files use board rules/defaults with a warning.
model::DesignRules read_design_rules(const std::string& board_path, const std::string& project_path = {});

// Parses a KiCad length with an optional unit suffix ("0.2mm", "8mil", "0.01in", "150um", "0.2") to nm.
std::optional<Coord> parse_length(std::string_view s);

}  // namespace tmk::io
