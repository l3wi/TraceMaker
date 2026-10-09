// SPDX-License-Identifier: GPL-3.0-or-later
#include "learn/knowledge_base.hpp"

#include <sqlite3.h>

#include <cmath>
#include <cstdlib>
#include <filesystem>

#include "core/rng.hpp"

namespace tmk::learn {

std::string BoardFeatures::bucket() const {
  // Layers, and log2 buckets of connection count and pad density.
  const int cb = connections > 0 ? static_cast<int>(std::log2(static_cast<double>(connections))) : 0;
  const double dens = area_cm2 > 0 ? static_cast<double>(pads) / area_cm2 : 0.0;
  const int db = dens > 0 ? static_cast<int>(std::log2(dens + 1.0)) : 0;
  return "L" + std::to_string(layers) + "-C" + std::to_string(cb) + "-D" + std::to_string(db);
}

BoardFeatures features_of(const model::Board& b, const std::string& file_text, int connections) {
  BoardFeatures f;
  std::uint64_t h = 0x243F6A8885A308D3ull;
  for (std::size_t i = 0; i < file_text.size(); i += 8) {
    std::uint64_t w = 0;
    for (std::size_t k = 0; k < 8 && i + k < file_text.size(); ++k) w |= static_cast<std::uint64_t>(static_cast<unsigned char>(file_text[i + k])) << (8 * k);
    h = splitmix64(h ^ w);
  }
  f.hash = h;
  f.layers = b.copper_count();
  for (const auto& n : b.nets) f.nets += !n.name.empty();
  f.pads = static_cast<int>(b.pads.size());
  f.connections = connections;
  const auto bb = b.edge_bbox();
  if (!bb.empty()) f.area_cm2 = nm_to_mm(bb.x1 - bb.x0) * nm_to_mm(bb.y1 - bb.y0) / 100.0;
  return f;
}

struct KnowledgeBase::Impl {
  sqlite3* db = nullptr;
  bool exec(const char* sql) { return db && sqlite3_exec(db, sql, nullptr, nullptr, nullptr) == SQLITE_OK; }
};

std::string KnowledgeBase::default_path() {
  const char* xdg = std::getenv("XDG_DATA_HOME");
  const char* home = std::getenv("HOME");
  const std::string base = xdg && *xdg ? std::string(xdg) : std::string(home ? home : ".") + "/.local/share";
  return base + "/tracemaker/kb.sqlite";
}

KnowledgeBase::KnowledgeBase(std::string path) : impl_(std::make_unique<Impl>()) {
  std::error_code ec;
  std::filesystem::create_directories(std::filesystem::path(path).parent_path(), ec);
  if (sqlite3_open(path.c_str(), &impl_->db) != SQLITE_OK) {
    sqlite3_close(impl_->db);
    impl_->db = nullptr;
    return;
  }
  sqlite3_busy_timeout(impl_->db, 2000);
  impl_->exec("PRAGMA journal_mode=WAL;");
  impl_->exec(
      "CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, board_hash TEXT, board TEXT, bucket TEXT, best_variant INTEGER,"
      " routed INTEGER, connections INTEGER, seconds REAL, created TEXT DEFAULT CURRENT_TIMESTAMP);"
      "CREATE TABLE IF NOT EXISTS variant_stats(bucket TEXT, variant INTEGER, plays INTEGER, wins INTEGER, PRIMARY KEY(bucket, variant));"
      "CREATE TABLE IF NOT EXISTS failed_connections(board_hash TEXT, net TEXT, pad_a TEXT, pad_b TEXT, fails INTEGER,"
      " PRIMARY KEY(board_hash, net, pad_a, pad_b));");
}

KnowledgeBase::~KnowledgeBase() {
  if (impl_->db) sqlite3_close(impl_->db);
}

bool KnowledgeBase::ok() const { return impl_->db != nullptr; }

namespace {
std::string hex(std::uint64_t v) {
  char b[17];
  std::snprintf(b, sizeof b, "%016llx", static_cast<unsigned long long>(v));
  return b;
}
}  // namespace

void KnowledgeBase::record_run(const BoardFeatures& f, const std::string& board_name, const std::vector<int>& variants_run, int best_variant,
                               int routed, int connections, double seconds) {
  if (!impl_->db) return;
  impl_->exec("BEGIN;");
  sqlite3_stmt* st = nullptr;
  if (sqlite3_prepare_v2(impl_->db, "INSERT INTO runs(board_hash, board, bucket, best_variant, routed, connections, seconds) VALUES(?,?,?,?,?,?,?)", -1,
                         &st, nullptr) == SQLITE_OK) {
    const std::string h = hex(f.hash), b = f.bucket();
    sqlite3_bind_text(st, 1, h.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(st, 2, board_name.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(st, 3, b.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_int(st, 4, best_variant);
    sqlite3_bind_int(st, 5, routed);
    sqlite3_bind_int(st, 6, connections);
    sqlite3_bind_double(st, 7, seconds);
    sqlite3_step(st);
  }
  sqlite3_finalize(st);
  for (int v : variants_run) {
    if (sqlite3_prepare_v2(impl_->db,
                           "INSERT INTO variant_stats(bucket, variant, plays, wins) VALUES(?,?,1,?) "
                           "ON CONFLICT(bucket, variant) DO UPDATE SET plays = plays + 1, wins = wins + excluded.wins",
                           -1, &st, nullptr) == SQLITE_OK) {
      const std::string b = f.bucket();
      sqlite3_bind_text(st, 1, b.c_str(), -1, SQLITE_TRANSIENT);
      sqlite3_bind_int(st, 2, v);
      sqlite3_bind_int(st, 3, v == best_variant ? 1 : 0);
      sqlite3_step(st);
    }
    sqlite3_finalize(st);
  }
  impl_->exec("COMMIT;");
}

int KnowledgeBase::failed_connection_count(std::uint64_t board_hash) const {
  int count = 0;
  if (!impl_->db) return count;
  sqlite3_stmt* st = nullptr;
  if (sqlite3_prepare_v2(impl_->db, "SELECT COUNT(*) FROM failed_connections WHERE board_hash = ?", -1, &st, nullptr) == SQLITE_OK) {
    const std::string h = hex(board_hash);
    sqlite3_bind_text(st, 1, h.c_str(), -1, SQLITE_TRANSIENT);
    if (sqlite3_step(st) == SQLITE_ROW) count = sqlite3_column_int(st, 0);
  }
  sqlite3_finalize(st);
  return count;
}

void KnowledgeBase::record_failures(std::uint64_t board_hash, const std::vector<FailedConnection>& failed) {
  if (!impl_->db) return;
  impl_->exec("BEGIN;");
  for (const auto& fc : failed) {
    sqlite3_stmt* st = nullptr;
    if (sqlite3_prepare_v2(impl_->db,
                           "INSERT INTO failed_connections(board_hash, net, pad_a, pad_b, fails) VALUES(?,?,?,?,1) "
                           "ON CONFLICT(board_hash, net, pad_a, pad_b) DO UPDATE SET fails = fails + 1",
                           -1, &st, nullptr) == SQLITE_OK) {
      const std::string h = hex(board_hash);
      sqlite3_bind_text(st, 1, h.c_str(), -1, SQLITE_TRANSIENT);
      sqlite3_bind_text(st, 2, fc.net.c_str(), -1, SQLITE_TRANSIENT);
      sqlite3_bind_text(st, 3, fc.pad_a.c_str(), -1, SQLITE_TRANSIENT);
      sqlite3_bind_text(st, 4, fc.pad_b.c_str(), -1, SQLITE_TRANSIENT);
      sqlite3_step(st);
    }
    sqlite3_finalize(st);
  }
  impl_->exec("COMMIT;");
}

}  // namespace tmk::learn
