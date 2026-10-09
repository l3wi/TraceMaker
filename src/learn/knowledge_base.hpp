// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
// Persistent knowledge base (design doc 06 §4, failure-memory tier T3): what was learned on earlier runs.
//
//  * per board-feature bucket: variant outcomes for reporting and offline analysis;
//  * per board (exact content hash): connections left unrouted on earlier runs.
// History never changes connection order or the configured portfolio (D99).
//
// SQLite file, default ~/.local/share/tracemaker/kb.sqlite. Every call is best-effort: a missing or locked
// database never stops routing.
#include <cstdint>
#include <memory>
#include <string>
#include <vector>

#include "model/board.hpp"

namespace tmk::learn {

struct BoardFeatures {
  std::uint64_t hash = 0;   // content hash of the board file
  int layers = 0, nets = 0, pads = 0, connections = 0;
  double area_cm2 = 0;
  std::string bucket() const;  // coarse class used to share statistics between similar boards
};

BoardFeatures features_of(const model::Board& b, const std::string& file_text, int connections);

struct FailedConnection {
  std::string net, pad_a, pad_b;  // "REF.NUM"
};

class KnowledgeBase {
 public:
  explicit KnowledgeBase(std::string path = default_path());
  ~KnowledgeBase();
  bool ok() const;
  static std::string default_path();

  void record_run(const BoardFeatures& f, const std::string& board_name, const std::vector<int>& variants_run, int best_variant,
                  int routed, int connections, double seconds);

  int failed_connection_count(std::uint64_t board_hash) const;
  void record_failures(std::uint64_t board_hash, const std::vector<FailedConnection>& failed);

 private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
};

}  // namespace tmk::learn
