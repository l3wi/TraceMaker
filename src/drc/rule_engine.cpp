// SPDX-License-Identifier: GPL-3.0-or-later
#include "drc/rule_engine.hpp"
#include "drc/rule_geometry.hpp"

#include <algorithm>
#include <cctype>
#include <cstdlib>
#include <cmath>
#include <bit>
#include <limits>
#include <string_view>

namespace tmk::drc {

// ---------------------------------------------------------------------------------------------------------
// Condition expressions: a small recursive-descent parser for the subset of KiCad's rule language used in
// practice: A./B. properties, string/number literals, comparisons, && || ! and parentheses, and a few functions.
// ---------------------------------------------------------------------------------------------------------
struct EvalCtx {
  const RuleEngine* eng;
  const CopperItem* a;
  const CopperItem* b;
  int layer;
  bool unknown = false;  // set when an unsupported property or function is evaluated
  bool reference = false;
};

struct Value {
  enum class K { Undef, Bool, Str, Num } k = K::Undef;
  bool b = false;
  std::string_view s;  // borrowed from immutable board/AST strings
  double n = 0;
  bool truthy() const { return k == K::Bool ? b : k == K::Num ? n != 0 : k == K::Str ? !s.empty() : false; }
};

class Condition {
 public:
  struct Node {
    enum class Op { Or, And, Not, Eq, Ne, Lt, Le, Gt, Ge, Lit, Prop, Call } op;
    std::vector<std::unique_ptr<Node>> kids;
    Value lit;
    std::string text;  // owns literal string storage
    char who = 'A';       // A or B
    std::string name;     // property or function name
    std::vector<std::string> args;
    std::vector<std::size_t> regions;  // bound geometric leaves; no selector work in the routing hot path
  };

  static std::unique_ptr<Condition> parse(const std::string& text, model::RuleOrigin origin, std::string& err) {
    auto c = std::make_unique<Condition>();
    c->src_ = text;
    c->integer_lengths_ = origin == model::RuleOrigin::Synthetic;
    c->pos_ = 0;
    try {
      c->root_ = c->parse_or();
      c->skip();
      if (c->pos_ != c->src_.size()) throw std::runtime_error("unexpected '" + c->src_.substr(c->pos_, 10) + "'");
    } catch (const std::exception& e) {
      err = e.what();
      return nullptr;
    }
    return c;
  }

  bool eval(EvalCtx& ctx) const { return eval(*root_, ctx).truthy(); }
  enum Dependency : unsigned { Static = 0, Net = 1, Item = 2, Spatial = 4, Anchor = 8, Membership = 16 };
  struct Symbol { std::string_view name; bool call; unsigned dependencies; int arity; };
  // One structural registry: capability checking must visit even short-circuited branches.
  static constexpr Symbol symbols[] = {
      {"NetClass", false, Net, 0}, {"NetName", false, Net, 0}, {"Type", false, Static, 0},
      {"Layer", false, Item, 0}, {"L", false, Static, 0}, {"Reference", false, Item, 0},
      {"Parent.Reference", false, Item, 0}, {"Pad_Type", false, Item, 0},
      {"Size_X", false, Item, 0}, {"Size_Y", false, Item, 0}, {"Width", false, Item, 0},
      {"Position_X", false, Item | Anchor, 0}, {"Position_Y", false, Item | Anchor, 0},
      {"isPlated", true, Static, 0}, {"existsOnLayer", true, Item, 1},
      {"insideArea", true, Item | Spatial, 1}, {"intersectsArea", true, Item | Spatial, 1},
      {"enclosedByArea", true, Item | Spatial | Anchor, 1},
      {"insideCourtyard", true, Item | Spatial, 1}, {"intersectsCourtyard", true, Item | Spatial, 1},
      {"insideFrontCourtyard", true, Item | Spatial, 1}, {"intersectsFrontCourtyard", true, Item | Spatial, 1},
      {"insideBackCourtyard", true, Item | Spatial, 1}, {"intersectsBackCourtyard", true, Item | Spatial, 1},
      {"inDiffPair", true, Net, 1}, {"memberOfFootprint", true, Item | Membership, 1}};
  struct Capabilities {
    unsigned dependencies = Static;
    std::vector<std::string> unsupported;
    bool non_monotone = false;
    bool unknown_except_membership = false;
  };
  Capabilities capabilities() const {
    Capabilities out;
    collect(*root_, out);
    out.non_monotone = non_monotone(*root_);
    return out;
  }
  void validate_geometry(const RuleGeometry& geometry, Capabilities& capabilities) const {
    validate_geometry(*root_, geometry, capabilities);
  }
  void bind_geometry(const RuleGeometry& geometry) { bind_geometry(*root_, geometry); }
  std::optional<bool> static_result(EvalCtx& ctx) const { return static_result(*root_, ctx); }

 private:
  void skip() {
    while (pos_ < src_.size() && std::isspace(static_cast<unsigned char>(src_[pos_]))) ++pos_;
  }
  bool eat(std::string_view tok) {
    skip();
    if (src_.compare(pos_, tok.size(), tok) == 0) {
      pos_ += tok.size();
      return true;
    }
    return false;
  }
  std::unique_ptr<Node> make(Node::Op op) {
    auto n = std::make_unique<Node>();
    n->op = op;
    return n;
  }
  std::unique_ptr<Node> parse_or() {
    auto l = parse_and();
    while (eat("||")) {
      auto n = make(Node::Op::Or);
      n->kids.push_back(std::move(l));
      n->kids.push_back(parse_and());
      l = std::move(n);
    }
    return l;
  }
  std::unique_ptr<Node> parse_and() {
    auto l = parse_unary();
    while (eat("&&")) {
      auto n = make(Node::Op::And);
      n->kids.push_back(std::move(l));
      n->kids.push_back(parse_unary());
      l = std::move(n);
    }
    return l;
  }
  std::unique_ptr<Node> parse_unary() {
    skip();
    if (pos_ < src_.size() && src_[pos_] == '!' && (pos_ + 1 >= src_.size() || src_[pos_ + 1] != '=')) {
      ++pos_;
      auto n = make(Node::Op::Not);
      n->kids.push_back(parse_unary());
      return n;
    }
    auto l = parse_term();
    for (const auto& [token, op] : {std::pair{"==", Node::Op::Eq}, {"!=", Node::Op::Ne},
                                  {"<=", Node::Op::Le}, {">=", Node::Op::Ge},
                                  {"<", Node::Op::Lt}, {">", Node::Op::Gt}}) {
      if (!eat(token)) continue;
      auto n = make(op);
      n->kids.push_back(std::move(l));
      n->kids.push_back(parse_term());
      return n;
    }
    return l;
  }
  std::string read_string() {
    const char q = src_[pos_++];
    std::string s;
    while (pos_ < src_.size() && src_[pos_] != q) s += src_[pos_++];
    if (pos_ >= src_.size()) throw std::runtime_error("unterminated string");
    ++pos_;
    return s;
  }
  std::unique_ptr<Node> parse_term() {
    skip();
    if (pos_ >= src_.size()) throw std::runtime_error("unexpected end");
    const char c = src_[pos_];
    if (c == '(') {
      ++pos_;
      auto n = parse_or();
      if (!eat(")")) throw std::runtime_error("missing ')'");
      return n;
    }
    if (c == '\'' || c == '"') {
      auto n = make(Node::Op::Lit);
      n->lit.k = Value::K::Str;
      n->text = read_string();
      n->lit.s = n->text;
      return n;
    }
    if (std::isdigit(static_cast<unsigned char>(c)) || c == '-' || c == '+' || c == '.') {
      char* end = nullptr;
      const double number = std::strtod(src_.c_str() + pos_, &end);
      if (end == src_.c_str() + pos_ || !std::isfinite(number)) throw std::runtime_error("invalid number");
      pos_ = static_cast<std::size_t>(end - src_.c_str());
      skip();
      const std::size_t b = pos_;
      while (pos_ < src_.size() && std::isalpha(static_cast<unsigned char>(src_[pos_]))) ++pos_;
      const std::string_view unit(src_.data() + b, pos_ - b);
      // KiCad 10 PCBEXPR_UNIT_RESOLVER: lengths use nm; bare numbers are unscaled.
      // https://gitlab.com/kicad/code/kicad/-/blob/10.0.3/pcbnew/pcbexpr_evaluator.cpp
      double scale = 1;
      if (unit == "mm") scale = 1'000'000;
      else if (unit == "mil") scale = 25'400;
      else if (unit == "in") scale = 25'400'000;
      else if (unit == "ps") scale = 1'000;
      else if (!unit.empty() && unit != "deg" && unit != "fs") throw std::runtime_error("unsupported unit");
      auto n = make(Node::Op::Lit);
      n->lit.k = Value::K::Num;
      n->lit.n = number * scale;
      if (!std::isfinite(n->lit.n)) throw std::runtime_error("invalid number");
      if (integer_lengths_ && (unit == "mm" || unit == "mil" || unit == "in")) {
        if (n->lit.n < static_cast<double>(std::numeric_limits<Coord>::min()) ||
            n->lit.n >= static_cast<double>(std::numeric_limits<Coord>::max()))
          throw std::runtime_error("length literal out of range");
        // Synthetic geometry boundaries use integer nm; project literals retain KiCad's double semantics.
        n->lit.n = static_cast<double>(std::llround(n->lit.n));
      }
      return n;
    }
    // Identifier: A.Prop, B.func(args), or a bare word (true/false).
    std::size_t b = pos_;
    while (pos_ < src_.size() && (std::isalnum(static_cast<unsigned char>(src_[pos_])) || src_[pos_] == '_' || src_[pos_] == '.')) ++pos_;
    std::string id = src_.substr(b, pos_ - b);
    if (id.empty()) throw std::runtime_error(std::string("unexpected '") + c + "'");
    if (id == "true" || id == "false") {
      auto n = make(Node::Op::Lit);
      n->lit.k = Value::K::Bool;
      n->lit.b = id == "true";
      return n;
    }
    auto n = make(Node::Op::Prop);
    n->who = '\0';
    if (id.size() > 2 && (id[0] == 'A' || id[0] == 'B') && id[1] == '.') {
      n->who = id[0];
      n->name = id.substr(2);
    } else {
      n->name = id;
    }
    if (eat("(")) {
      n->op = Node::Op::Call;
      skip();
      while (!eat(")")) {
        skip();
        if (pos_ < src_.size() && (src_[pos_] == '\'' || src_[pos_] == '"')) n->args.push_back(read_string());
        else throw std::runtime_error("function arguments must be strings");
        eat(",");
      }
    }
    return n;
  }

  static bool spatial(const Node& n) {
    if (n.op == Node::Op::Prop && n.name == "Width") return true;
    if (n.op == Node::Op::Call)
      for (const auto& s : symbols)
        if (s.name == n.name && (s.dependencies & Spatial)) return true;
    for (const auto& k : n.kids)
      if (spatial(*k)) return true;
    return false;
  }
  static bool non_monotone(const Node& n) {
    // Odd track widths round down in search disks. Increasing width predicates stay monotone.
    if (n.kids.size() == 2) {
      const auto width = [](const Node& node) { return node.op == Node::Op::Prop && node.name == "Width"; };
      const auto number = [](const Node& node) { return node.op == Node::Op::Lit && node.lit.k == Value::K::Num; };
      if (((n.op == Node::Op::Gt || n.op == Node::Op::Ge) && width(*n.kids[0]) && number(*n.kids[1])) ||
          ((n.op == Node::Op::Lt || n.op == Node::Op::Le) && number(*n.kids[0]) && width(*n.kids[1]))) return false;
    }
    // Arbitrary Boolean comparison/negation of an intersection is not a forbidden-region leaf.
    if (n.op != Node::Op::Or && n.op != Node::Op::And && n.op != Node::Op::Call && spatial(n)) return true;
    for (const auto& k : n.kids)
      if (non_monotone(*k)) return true;
    return false;
  }
  static void collect(const Node& n, Capabilities& out) {
    if (n.op == Node::Op::Prop || n.op == Node::Op::Call) {
      const bool call = n.op == Node::Op::Call;
      const auto* found = std::find_if(std::begin(symbols), std::end(symbols), [&](const Symbol& s) {
        return s.name == n.name && s.call == call && (n.who != '\0' || n.name == "L") &&
               (n.name != "L" || n.who == '\0') && static_cast<int>(n.args.size()) == s.arity;
      });
      if (found != std::end(symbols)) out.dependencies |= found->dependencies;
      else {
        const std::string symbol = (n.who ? std::string(1, n.who) + "." : "") + n.name;
        if (std::find(out.unsupported.begin(), out.unsupported.end(), symbol) == out.unsupported.end())
          out.unsupported.push_back(symbol);
        out.dependencies |= Item;
        if (n.name != "memberOfGroup" || !call || n.args.size() != 1) out.unknown_except_membership = true;
      }
      if (call && !n.args.empty() && n.args.front().find("${Class:") != std::string::npos) {
        out.unsupported.push_back(n.name + " selector " + n.args.front());
        if (n.name != "memberOfFootprint") out.unknown_except_membership = true;
      }
    }
    for (const auto& k : n.kids) collect(*k, out);
  }
  static void validate_geometry(const Node& n, const RuleGeometry& geometry, Capabilities& capabilities) {
    if (n.op == Node::Op::Call && n.args.size() == 1) {
      std::vector<std::string> diagnostics;
      if (n.name == "insideArea" || n.name == "intersectsArea" || n.name == "enclosedByArea")
        diagnostics = geometry.validate_area(n.args[0]);
      else if (n.name.find("Courtyard") != std::string::npos)
        diagnostics = geometry.validate_courtyard(n.args[0]);
      for (const auto& diagnostic : diagnostics) {
        capabilities.unsupported.push_back(n.name + ": " + diagnostic);
        capabilities.unknown_except_membership = true;
      }
    }
    for (const auto& k : n.kids) validate_geometry(*k, geometry, capabilities);
  }
  static void bind_geometry(Node& n, const RuleGeometry& geometry) {
    if (n.op == Node::Op::Call && n.args.size() == 1) {
      if (n.name == "insideArea" || n.name == "intersectsArea" || n.name == "enclosedByArea")
        n.regions = geometry.area_regions(n.args[0]);
      else if (n.name.find("Courtyard") != std::string::npos) {
        const int side = n.name.find("Front") != std::string::npos ? 1 : n.name.find("Back") != std::string::npos ? 2 : 0;
        n.regions = geometry.courtyard_regions(n.args[0], side);
      }
    }
    for (auto& k : n.kids) bind_geometry(*k, geometry);
  }
  static bool fully_static(const Node& n) {
    if (n.op == Node::Op::Prop)
      return n.who == 'B' || n.name == "NetName" || n.name == "NetClass" || n.name == "Type" ||
             n.name == "L" || n.name == "Reference" || n.name == "Parent.Reference" ||
             n.name == "Pad_Type" || n.name == "Size_X" || n.name == "Size_Y";
    if (n.op == Node::Op::Call)
      return n.who == 'B' || n.name == "inDiffPair" || n.name == "isPlated" ||
             n.name == "memberOfFootprint" || n.name == "memberOfGroup";
    return std::all_of(n.kids.begin(), n.kids.end(), [](const auto& k) { return fully_static(*k); });
  }
  std::optional<bool> static_result(const Node& n, EvalCtx& ctx) const {
    if (n.op == Node::Op::And || n.op == Node::Op::Or) {
      const auto a = static_result(*n.kids[0], ctx), b = static_result(*n.kids[1], ctx);
      if (n.op == Node::Op::And) {
        if ((a && !*a) || (b && !*b)) return false;
        if (a && b) return true;
      } else {
        if ((a && *a) || (b && *b)) return true;
        if (a && b) return false;
      }
      return std::nullopt;
    }
    if (n.op == Node::Op::Not) {
      const auto a = static_result(*n.kids[0], ctx);
      return a ? std::optional<bool>(!*a) : std::nullopt;
    }
    return fully_static(n) ? std::optional<bool>(eval(n, ctx).truthy()) : std::nullopt;
  }


  static bool str_eq(const Value& l, const Value& r) {
    if (l.k == Value::K::Num || r.k == Value::K::Num) {
      const double a = l.k == Value::K::Num ? l.n : l.s.empty() ? 0 : std::atof(l.s.data());
      const double b = r.k == Value::K::Num ? r.n : r.s.empty() ? 0 : std::atof(r.s.data());
      return a == b;  // KiCad VALUE::EqualTo compares numeric doubles without a tolerance.
    }
    if (l.k == Value::K::Bool || r.k == Value::K::Bool) return l.truthy() == r.truthy();
    // KiCad compares strings case-insensitively with wildcard support on either side.
    auto match = [](std::string_view pattern, std::string_view text) {
      auto lower = [](char ch) { return std::tolower(static_cast<unsigned char>(ch)); };
      std::size_t pi = 0, ti = 0, star = std::string_view::npos, mark = 0;
      while (ti < text.size()) {
        if (pi < pattern.size() && (pattern[pi] == '?' || lower(pattern[pi]) == lower(text[ti]))) {
          ++pi; ++ti;
        } else if (pi < pattern.size() && pattern[pi] == '*') {
          star = pi++; mark = ti;
        } else if (star != std::string_view::npos) {
          pi = star + 1; ti = ++mark;
        } else return false;
      }
      while (pi < pattern.size() && pattern[pi] == '*') ++pi;
      return pi == pattern.size();
    };
    return match(l.s, r.s) || match(r.s, l.s);
  }

  Value prop(const Node& n, EvalCtx& ctx) const {
    const CopperItem* it = n.who == 'A' ? ctx.a : ctx.b;
    Value v;
    if (n.name == "L" && n.who == '\0') {
      v.k = Value::K::Str;
      if (ctx.layer >= 0) v.s = ctx.eng->b_.copper_name(ctx.layer);
      return v;
    }
    if (!it) return v;  // B absent (single-item constraint): undefined → false
    // A supported property absent on this item is undefined, not an unknown capability.
    if (((n.name == "Pad_Type" || n.name == "Size_X" || n.name == "Size_Y") && it->kind != ItemKind::Pad) ||
        ((n.name == "Reference" || n.name == "Parent.Reference") && it->footprint < 0) ||
        (n.name == "Width" && (it->kind == ItemKind::Pad || it->kind == ItemKind::Zone))) return v;
    const auto& b = ctx.eng->b_;
    v.k = Value::K::Str;
    if (n.name == "NetClass") v.s = ctx.eng->netclass(*it).name;
    else if (n.name == "NetName") v.s = b.nets[static_cast<std::size_t>(it->net)].name;
    else if (n.name == "Type") v.s = it->kind == ItemKind::Arc ? "Track" : kind_name(it->kind);
    else if (n.name == "Layer") {
      // BOARD_ITEM's Layer property is GetLayer(), not PCBEXPR_CONTEXT::GetLayer() (bare L).
      // KiCad 10.0.3 pcbexpr_evaluator.cpp and board_item.cpp property registration.
      const int l = it->anchor_layer >= 0 ? it->anchor_layer :
                    it->layers ? std::countr_zero(it->layers) : -1;
      if (l >= 0) v.s = b.copper_name(l);
    }
    else if (n.name == "Reference" || n.name == "Parent.Reference")
      v.s = b.footprints[static_cast<std::size_t>(it->footprint)].reference;
    else if (n.name == "Pad_Type" && it->kind == ItemKind::Pad) {
      static const char* names[] = {"SMD", "Through-hole", "NPTH, mechanical", "Edge connector"};
      v.s = names[static_cast<int>(b.pads[static_cast<std::size_t>(it->index)].type)];
    } else if ((n.name == "Size_X" || n.name == "Size_Y") && it->kind == ItemKind::Pad) {
      // KiCad PAD::GetSizeX/GetSizeY return local padstack dimensions, independent of rotation.
      // https://gitlab.com/kicad/code/kicad/-/blob/10.0.3/pcbnew/pad.h
      const auto& pad = b.pads[static_cast<std::size_t>(it->index)];
      v.k = Value::K::Num;
      v.n = static_cast<double>(n.name == "Size_X" ? pad.size_x : pad.size_y);
    } else if (n.name == "Width") {
      v.k = Value::K::Num;
      v.n = static_cast<double>(it->width);
    } else if (n.name == "Position_X" || n.name == "Position_Y") {
      v.k = Value::K::Num;
      v.n = static_cast<double>(n.name == "Position_X" ? it->pos.x : it->pos.y);
    } else {
      ctx.unknown = true;
      v.k = Value::K::Undef;
    }
    return v;
  }

  Value call(const Node& n, EvalCtx& ctx) const {
    const CopperItem* it = n.who == 'A' ? ctx.a : ctx.b;
    Value v;
    v.k = Value::K::Bool;
    if (!it) return v;
    const auto& b = ctx.eng->b_;
    if (n.name == "isPlated") {
      v.b = it->kind == ItemKind::Via || (it->kind == ItemKind::Pad && b.pads[static_cast<std::size_t>(it->index)].type == model::PadType::ThruHole);
    } else if (n.name == "existsOnLayer" && !n.args.empty()) {
      const int l = b.copper_index(n.args[0]);
      v.b = l >= 0 && (it->layers & model::layer_bit(l));
    } else if ((n.name == "insideArea" || n.name == "intersectsArea" || n.name == "enclosedByArea") && n.args.size() == 1) {
      v.b = ctx.reference ? ctx.eng->geometry_->area_reference(*it, n.args[0], n.name == "enclosedByArea") :
                            ctx.eng->geometry_->area(*it, n.regions, n.name == "enclosedByArea");
    } else if ((n.name == "intersectsCourtyard" || n.name == "insideCourtyard" ||
                n.name == "intersectsFrontCourtyard" || n.name == "insideFrontCourtyard" ||
                n.name == "intersectsBackCourtyard" || n.name == "insideBackCourtyard") && n.args.size() == 1) {
      const int side = n.name.find("Front") != std::string::npos ? 1 : n.name.find("Back") != std::string::npos ? 2 : 0;
      v.b = ctx.reference ? ctx.eng->geometry_->courtyard_reference(*it, n.args[0], side) :
                            ctx.eng->geometry_->courtyard(*it, n.regions);
    } else if (n.name == "inDiffPair" && !n.args.empty()) {
      // KiCad: true when the item's net is one half of a differential pair whose base name (without the final
      // P/N or +/-) matches the pattern.
      const auto net = static_cast<std::size_t>(it->net);
      if (it->net != 0 && net < ctx.eng->dp_partner_.size() && ctx.eng->dp_partner_[net] != 0) {
        const std::string& name = b.nets[net].name;
        v.b = model::wildcard_match(n.args[0], std::string_view(name).substr(0, name.size() - 1)) || model::wildcard_match(n.args[0], name);
      }
    } else if (n.name == "memberOfFootprint" && !n.args.empty()) {
      if (it->footprint >= 0) {
        const auto& fp = b.footprints[static_cast<std::size_t>(it->footprint)];
        v.b = model::wildcard_match(n.args[0], n.args[0].find(':') != std::string::npos ? fp.lib_id : fp.reference);
      }
    } else if (n.name == "memberOfGroup" && n.args.size() == 1 && it->index < 0 &&
               (it->kind == ItemKind::Track || it->kind == ItemKind::Arc || it->kind == ItemKind::Via)) {
      v.b = false;  // Free router-created copper has neither a footprint nor group parent (KiCad 10.0.3).
    } else {
      ctx.unknown = true;
      v.k = Value::K::Undef;
    }
    return v;
  }

  Value eval(const Node& n, EvalCtx& ctx) const {
    Value v;
    v.k = Value::K::Bool;
    switch (n.op) {
      case Node::Op::Or: v.b = eval(*n.kids[0], ctx).truthy() || eval(*n.kids[1], ctx).truthy(); return v;
      case Node::Op::And: v.b = eval(*n.kids[0], ctx).truthy() && eval(*n.kids[1], ctx).truthy(); return v;
      case Node::Op::Not: v.b = !eval(*n.kids[0], ctx).truthy(); return v;
      case Node::Op::Eq:
      case Node::Op::Ne:
      case Node::Op::Lt:
      case Node::Op::Le:
      case Node::Op::Gt:
      case Node::Op::Ge: {
        const auto l = eval(*n.kids[0], ctx), r = eval(*n.kids[1], ctx);
        if (l.k == Value::K::Undef || r.k == Value::K::Undef) return v;
        if (n.op == Node::Op::Eq || n.op == Node::Op::Ne) {
          v.b = n.op == Node::Op::Eq ? str_eq(l, r) : !str_eq(l, r);
        } else if (l.k == Value::K::Num && r.k == Value::K::Num) {
          v.b = n.op == Node::Op::Lt ? l.n < r.n : n.op == Node::Op::Le ? l.n <= r.n :
                n.op == Node::Op::Gt ? l.n > r.n : l.n >= r.n;
        }
        return v;
      }
      case Node::Op::Lit: return n.lit;
      case Node::Op::Prop: return prop(n, ctx);
      case Node::Op::Call: return call(n, ctx);
    }
    return v;
  }

  std::string src_;
  std::size_t pos_ = 0;
  bool integer_lengths_ = false;
  std::unique_ptr<Node> root_;
};

// ---------------------------------------------------------------------------------------------------------

RuleEngine::RuleEngine(const model::Board& b, const model::DesignRules& r)
    : b_(b), r_(r) {
  if (r_.unreadable_custom_rules) {
    needs_exact_ = true;
    warnings_.push_back("custom rules could not be read; routing is disabled until the rule file is repaired");
  }
  for (const auto& rule : r_.custom) {
    Compiled c;
    c.rule = &rule;
    if (!rule.condition.empty()) {
      std::string err;
      c.cond = Condition::parse(rule.condition, rule.origin, err);
      if (!c.cond) {
        if (rule.origin == model::RuleOrigin::Synthetic)
          throw std::runtime_error("synthetic rule '" + rule.name + "': cannot parse condition (" + err + ")");
        c.valid = false;
        warnings_.push_back("rule '" + rule.name + "': cannot parse condition (" + err + "); conservatively assumed to match");
      }
    }
    Condition::Capabilities capabilities;
    if (c.cond) {
      capabilities = c.cond->capabilities();
      if (capabilities.dependencies & Condition::Spatial) {
        if (!geometry_) geometry_ = std::make_unique<RuleGeometry>(b);
        c.cond->validate_geometry(*geometry_, capabilities);
        c.cond->bind_geometry(*geometry_);
      }
    }
    c.item_dependent = capabilities.dependencies & Condition::Item;
    c.non_monotone = capabilities.non_monotone || (capabilities.dependencies & Condition::Anchor);
    c.unsupported = !capabilities.unsupported.empty();
    c.unknown_except_membership = capabilities.unknown_except_membership;
    for (const auto& symbol : capabilities.unsupported)
      warnings_.push_back("rule '" + rule.name + "': unsupported symbol " + symbol + "; conservatively assumed to match where unknown");
    const bool nets_seen = capabilities.dependencies & Condition::Net;
    for (const auto& k : rule.constraints) {
      if (k.type == "clearance" && k.min) max_clearance_ = std::max(max_clearance_, *k.min);
      if (k.type == "physical_hole_clearance" && k.min) max_physical_hole_ = std::max(max_physical_hole_, *k.min);
      // Static class-uniform disallows retain the existing caches. Item residuals and subtype spans do not.
      const bool subtype = k.type == "disallow" && std::any_of(k.items.begin(), k.items.end(), [](const auto& w) {
        return w == "through_via" || w == "blind_via" || w == "buried_via" || w == "micro_via" || w == "hole";
      });
      if (!c.valid || c.unsupported || (k.type == "disallow" ? c.item_dependent || subtype :
          !(k.type == "physical_hole_clearance" && !nets_seen))) needs_exact_ = true;
      if (k.type != "disallow") continue;
      for (const auto& w : k.items)
        if (w != "track" && w != "via" && w != "through_via" && w != "micro_via" && w != "buried_via" && w != "blind_via" && w != "pad" &&
            w != "zone" && w != "graphic")
          warnings_.push_back("rule '" + rule.name + "': unsupported disallow item " + w +
                              "; existing-item DRC coverage unavailable; hole predicates conservatively block new vias");
    }
    rules_.push_back(std::move(c));
  }
  for (const auto& c : r_.classes) max_clearance_ = std::max(max_clearance_, c.clearance);
  // Per-net caches: net class and diff-pair partner (string matching is far too slow for inner loops).
  net_class_.resize(b_.nets.size());
  dp_partner_.assign(b_.nets.size(), 0);
  std::map<std::string, model::NetId> by_name;
  for (const auto& n : b_.nets) by_name[n.name] = n.id;
  for (const auto& n : b_.nets) {
    net_class_[static_cast<std::size_t>(n.id)] = &r_.class_for(n.name);
    if (n.name.size() < 2) continue;
    std::string other = n.name;
    char& last = other.back();
    if (last == 'P') last = 'N';
    else if (last == 'N') last = 'P';
    else if (last == '+') last = '-';
    else if (last == '-') last = '+';
    else continue;
    if (auto it = by_name.find(other); it != by_name.end()) dp_partner_[static_cast<std::size_t>(n.id)] = it->second;
  }
  // Partial evaluation is per net and actual candidate kind, never a net-class representative.
  for (auto& c : rules_) {
    const bool disallow = std::any_of(c.rule->constraints.begin(), c.rule->constraints.end(),
                                     [](const auto& k) { return k.type == "disallow"; });
    if (!disallow || !c.valid || c.unknown_except_membership) continue;
    c.track_static.resize(b_.nets.size(), 2);
    c.via_static.resize(b_.nets.size(), 2);
    for (const auto& net : b_.nets)
      for (const auto kind : {ItemKind::Track, ItemKind::Via}) {
        CopperItem probe;
        probe.kind = kind;
        probe.net = net.id;
        EvalCtx context{this, &probe, nullptr, -1};
        const auto verdict = c.cond ? c.cond->static_result(context) : std::optional<bool>(true);
        auto& cache = kind == ItemKind::Track ? c.track_static : c.via_static;
        if (verdict) cache[static_cast<std::size_t>(net.id)] = *verdict ? 1 : 0;
      }
    std::map<std::string, std::pair<std::uint8_t, std::uint8_t>> class_verdicts;
    for (const auto& net : b_.nets) {
      const auto id = static_cast<std::size_t>(net.id);
      const auto verdict = std::pair{c.track_static[id], c.via_static[id]};
      const auto [previous, inserted] = class_verdicts.emplace(net_class_[id]->name, verdict);
      if (!inserted && previous->second != verdict) needs_exact_ = true;
    }
  }
  for (const auto& rule : r_.custom)
    for (const auto& k : rule.constraints)
      if (k.type == "clearance") any_custom_clearance_ = true;
  for (const auto& p : b_.pads) max_clearance_ = std::max(max_clearance_, p.clearance);
  max_clearance_ = std::max({max_clearance_, r_.minimums.clearance, r_.minimums.copper_edge_clearance, r_.minimums.hole_clearance,
                             r_.minimums.hole_to_hole});
}

RuleEngine::~RuleEngine() = default;

void RuleEngine::use_zone_clearance_overrides() {
  zone_overrides_ = true;
  for (const auto& z : b_.zones)
    if (!z.rule_area) max_clearance_ = std::max(max_clearance_, z.clearance);
}

const model::NetClass& RuleEngine::netclass(const CopperItem& it) const {
  const auto i = static_cast<std::size_t>(it.net);
  if (i < net_class_.size()) return *net_class_[i];
  return r_.class_for(b_.nets[i].name);
}

bool RuleEngine::layer_matches(const std::string& sel, int layer) const {
  if (sel.empty() || layer < 0) return true;
  const int last = b_.copper_count() - 1;
  if (sel == "outer") return layer == 0 || layer == last;
  if (sel == "inner") return layer > 0 && layer < last;
  return b_.copper_index(sel) == layer;
}
bool RuleEngine::condition_matches(const Compiled& c, const CopperItem* a, const CopperItem* b, int layer, bool reference) const {
  // An unreadable exception cannot relax a known prohibition or minimum.
  if (!c.valid || (c.unsupported && (c.unknown_except_membership || !a || a->index >= 0)))
    return c.rule->severity != "ignore";
  if (!c.cond) return true;
  EvalCtx ctx{this, a, b, layer, false, reference};
  const bool match = c.cond->eval(ctx);
  return ctx.unknown ? c.rule->severity != "ignore" : match;
}


std::optional<Coord> RuleEngine::custom_min(const char* type, const CopperItem* a, const CopperItem* b, int layer) const {
  std::optional<Coord> out;
  std::optional<Coord> conservative_min;
  std::optional<Coord> synthetic_min;
  for (const auto& c : rules_) {
    if (!layer_matches(c.rule->layer, layer)) continue;
    const model::Constraint* k = nullptr;
    for (const auto& x : c.rule->constraints)
      if (x.type == type && x.min) k = &x;
    if (!k) continue;
    const bool uncertain = !c.valid || c.unsupported;
    const bool match = condition_matches(c, a, b, layer) || (b && condition_matches(c, b, a, layer));
    if (match) {
      if (uncertain)
        conservative_min = std::max(conservative_min.value_or(*k->min), *k->min);
      else if (c.rule->origin == model::RuleOrigin::Synthetic)
        synthetic_min = std::max(synthetic_min.value_or(*k->min), *k->min);
      else
        out = c.rule->severity == "ignore" ? 0 : *k->min;
    }
  }
  // A route preference may strengthen, but never replace or weaken, the project's selected minimum.
  if (synthetic_min) out = std::max(out.value_or(*synthetic_min), *synthetic_min);
  if (conservative_min) out = std::max(out.value_or(*conservative_min), *conservative_min);
  return out;
}

std::optional<Coord> RuleEngine::custom_max(const char* type, const CopperItem* a, const CopperItem* b, int layer) const {
  std::optional<Coord> out, conservative;
  for (const auto& c : rules_) {
    if (!layer_matches(c.rule->layer, layer)) continue;
    const model::Constraint* k = nullptr;
    for (const auto& x : c.rule->constraints)
      if (x.type == type && x.max) k = &x;
    if (!k || !condition_matches(c, a, b, layer)) continue;
    if (!c.valid || c.unsupported || c.rule->origin == model::RuleOrigin::Synthetic)
      conservative = std::min(conservative.value_or(*k->max), *k->max);
    else
      out = c.rule->severity == "ignore" ? std::nullopt : k->max;
  }
  if (conservative) out = std::min(out.value_or(*conservative), *conservative);
  return out;
}

std::pair<std::optional<Coord>, std::optional<Coord>> RuleEngine::length_constraint(model::NetId net) const {
  const auto constraint = net_constraint(net, "length");
  return constraint ? std::pair{constraint->min, constraint->max} :
                      std::pair<std::optional<Coord>, std::optional<Coord>>{};
}

std::optional<Coord> RuleEngine::skew_constraint(model::NetId net) const {
  const auto constraint = net_constraint(net, "skew");
  return constraint ? constraint->max : std::nullopt;
}

std::optional<model::Constraint> RuleEngine::net_constraint(model::NetId net, const std::string& type) const {
  std::optional<model::Constraint> out;
  model::Constraint conservative;
  CopperItem probe;
  probe.kind = ItemKind::Track;
  probe.net = net;
  probe.layers = ~model::LayerMask{0};
  for (const auto& c : rules_) {
    const model::Constraint* k = nullptr;
    for (const auto& x : c.rule->constraints)
      if (x.type == type) k = &x;
    if (!k) continue;
    if (!condition_matches(c, &probe, nullptr, -1)) continue;
    if (!c.valid || c.unsupported) {
      if (k->min) conservative.min = std::max(conservative.min.value_or(*k->min), *k->min);
      if (k->max) conservative.max = std::min(conservative.max.value_or(*k->max), *k->max);
      continue;
    }
    out = *k;  // later rules take precedence
  }
  if (conservative.min || conservative.max) {
    if (!out) { out.emplace(); out->type = type; }
    if (conservative.min) out->min = std::max(out->min.value_or(*conservative.min), *conservative.min);
    if (conservative.max) out->max = std::min(out->max.value_or(*conservative.max), *conservative.max);
  }
  return out;
}

namespace {
// KiCad's disallow item keywords (DRC_RULES_PARSER): "via" covers every via type.
bool disallow_word_matches(const std::string& w, const CopperItem& it, const model::Board& b) {
  switch (it.kind) {
    case ItemKind::Track:
    case ItemKind::Arc: return w == "track";
    case ItemKind::Pad: return w == "pad";
    case ItemKind::Zone: return w == "zone";
    case ItemKind::Graphic: return w == "graphic";
    case ItemKind::Via: {
      if (w == "via") return true;
      const auto type = it.index >= 0 ? b.vias[static_cast<std::size_t>(it.index)].type : it.via_type;
      const auto outer = model::layer_bit(0) | model::layer_bit(b.copper_count() - 1);
      const bool blind = type == model::ViaType::Blind && (it.layers & outer);
      const bool buried = type == model::ViaType::Blind && !(it.layers & outer);
      return (w == "through_via" && type == model::ViaType::Through) || (w == "micro_via" && type == model::ViaType::Micro) ||
             (w == "buried_via" && buried) || (w == "blind_via" && blind) ||
             (w == "hole" && it.index < 0);  // unsupported hole predicates must not admit newly created holes
    }
  }
  return false;
}
}  // namespace

bool RuleEngine::disallow_hit(const Compiled& c, const CopperItem& it, int layer, bool reference) const {
  if (!c.rule->layer.empty()) {
    bool shared = false;
    for (auto mask = it.layers; mask; mask &= mask - 1)
      shared = shared || layer_matches(c.rule->layer, std::countr_zero(mask));
    if (!shared) return false;
  }
  bool typed = false;
  for (const auto& k : c.rule->constraints)
    if (k.type == "disallow")
      for (const auto& w : k.items) typed = typed || disallow_word_matches(w, it, b_);
  if (!typed) return false;
  if (!reference && it.index < 0 && (it.kind == ItemKind::Track || it.kind == ItemKind::Arc || it.kind == ItemKind::Via)) {
    const auto& cache = it.kind == ItemKind::Via ? c.via_static : c.track_static;
    const auto net = static_cast<std::size_t>(it.net);
    if (net < cache.size() && cache[net] < 2) return cache[net] != 0;
  }
  (void) layer;
  // KiCad's disallow provider evaluates unary constraints with UNDEFINED_LAYER, then filters item layers.
  return condition_matches(c, &it, nullptr, -1, reference);
}

const RuleEngine::Compiled* RuleEngine::disallow_rule(const CopperItem& it, int layer, bool search, bool reference) const {
  const Compiled* out = nullptr;
  if (search)
    for (const auto& c : rules_)
      if (c.valid && !c.unsupported && c.item_dependent && c.rule->severity == "ignore" &&
          std::any_of(c.rule->constraints.begin(), c.rule->constraints.end(), [](const auto& k) { return k.type == "disallow"; }))
        return nullptr;  // a whole-item spatial exception may rescue the geometry-less probe
  for (const auto& c : rules_) {
    if (search && c.item_dependent) continue;
    if (disallow_hit(c, it, layer, reference))
      out = c.rule->severity == "ignore" ? nullptr : &c;
  }
  return out;
}

std::optional<std::string> RuleEngine::disallowed(const CopperItem& it, int layer) const {
  const auto* c = disallow_rule(it, layer, false);
  return c ? std::optional<std::string>(c->rule->name) : std::nullopt;
}
std::string_view RuleEngine::disallow_severity(const CopperItem& it, int layer) const {
  const auto* c = disallow_rule(it, layer, false);
  return c && !c->rule->severity.empty() ? std::string_view(c->rule->severity) : std::string_view("error");
}


bool RuleEngine::candidate_allowed(const CopperItem& it, int layer) const {
  return !r_.unreadable_custom_rules && !disallow_rule(it, layer, false);
}
bool RuleEngine::candidate_allowed_reference(const CopperItem& it, int layer) const {
  return !r_.unreadable_custom_rules && !disallow_rule(it, layer, false, true);
}


bool RuleEngine::candidate_search_allowed(const CopperItem& it, int layer) const {
  if (r_.unreadable_custom_rules) return false;
  // Disk rejection is valid only for monotone prohibitions; an ignored spatial exception may rescue a
  // whole segment even when its endpoint disk is forbidden by an earlier rule.
  for (const auto& c : rules_)
    if (c.non_monotone || (c.item_dependent && c.rule->severity == "ignore")) return true;
  return !disallow_rule(it, layer, false);
}

bool RuleEngine::track_allowed(model::NetId net, int layer) const {
  if (r_.unreadable_custom_rules) return false;
  CopperItem probe;
  probe.kind = ItemKind::Track;
  probe.net = net;
  probe.layers = model::layer_bit(layer);
  probe.anchor_layer = layer;
  return !disallow_rule(probe, layer, true);
}

bool RuleEngine::via_allowed(model::NetId net) const {
  if (r_.unreadable_custom_rules) return false;
  for (const auto& c : rules_)
    for (const auto& k : c.rule->constraints)
      if (k.type == "disallow" && std::any_of(k.items.begin(), k.items.end(), [](const auto& word) {
            return word == "through_via" || word == "blind_via" || word == "buried_via" || word == "micro_via";
          })) return true;  // through-via permission alone cannot rule out every possible span/type
  CopperItem probe;
  probe.kind = ItemKind::Via;
  probe.net = net;
  probe.anchor_layer = 0;
  for (int l = 0; l < b_.copper_count(); ++l) probe.layers |= model::layer_bit(l);
  return !disallow_rule(probe, -1, true);
}

Coord RuleEngine::physical_hole_clearance(const CopperItem* hole_owner, const CopperItem& other, int layer) const {
  if (max_physical_hole_ <= 0) return -1;
  return custom_min("physical_hole_clearance", hole_owner, &other, layer).value_or(-1);
}

bool RuleEngine::coupled_diff_pair(model::NetId a, model::NetId b) const {
  if (a == 0 || b == 0 || a == b) return false;
  if (static_cast<std::size_t>(a) < dp_partner_.size()) return dp_partner_[static_cast<std::size_t>(a)] == b;
  const std::string& x = b_.nets[static_cast<std::size_t>(a)].name;
  const std::string& y = b_.nets[static_cast<std::size_t>(b)].name;
  if (x.size() != y.size() || x.empty()) return false;
  const char cx = x.back(), cy = y.back();
  const bool pair = (cx == 'P' && cy == 'N') || (cx == 'N' && cy == 'P') || (cx == '+' && cy == '-') || (cx == '-' && cy == '+');
  return pair && x.compare(0, x.size() - 1, y, 0, y.size() - 1) == 0;
}

Coord RuleEngine::clearance(const CopperItem& a, const CopperItem& b, int layer) const {
  // Local overrides (pad, then footprint) win over net classes: KiCad uses the larger of the two items'
  // overrides when either has one (verified on tinytapeout-demo: U6 footprint 0.18 mm vs GND class 0.23 mm).
  auto local = [&](const CopperItem& it) -> Coord {
    if (it.kind != ItemKind::Pad) return -1;
    const auto& pad = b_.pads[static_cast<std::size_t>(it.index)];
    if (pad.clearance >= 0) return pad.clearance;
    return b_.footprints[static_cast<std::size_t>(pad.footprint)].clearance;
  };
  const Coord la = local(a), lb = local(b);
  Coord req = (la >= 0 || lb >= 0) ? std::max(la, lb) : std::max(netclass(a).clearance, netclass(b).clearance);
  // KiCad's implicit diff-pair rule: between the two halves of a coupled pair, the class's diff-pair gap
  // replaces the clearance (verified on CM5_MINIMA_3: PCIe P/N at 0.177 mm under a 0.2 mm class clearance).
  // Observed: the smaller of clearance and diff-pair gap applies (0.154 gap passes under a 0.2 class; 0.2 mm
  // pads pass under a 0.25 gap).
  if (la < 0 && lb < 0 && coupled_diff_pair(a.net, b.net)) req = std::min(req, netclass(a).diff_pair_gap);
  bool custom = false;
  if (any_custom_clearance_)
    if (auto c = custom_min("clearance", &a, &b, layer)) req = *c, custom = true;
  // A copper zone's own clearance is a local clearance: without a pad override or a custom rule, the larger of
  // it and the net-class value applies (DRC_ENGINE::EvalRules; KiCad names it "zone clearance").
  if (zone_overrides_ && la < 0 && lb < 0 && !custom)
    for (const CopperItem* z : {&a, &b})
      if (z->kind == ItemKind::Zone) req = std::max(req, b_.zones[static_cast<std::size_t>(z->index)].clearance);
  // The board minimum is always a floor (multichannel_mixer: 0.3 mm minimum over a 0.2 mm class).
  return std::max(req, r_.minimums.clearance);
}

Coord RuleEngine::hole_clearance(const CopperItem* owner, const CopperItem& other, int layer) const {
  Coord req = r_.minimums.hole_clearance;
  if (auto c = custom_min("hole_clearance", owner, &other, layer)) req = std::max(req, *c);
  return req;
}

Coord RuleEngine::hole_to_hole(const CopperItem* a, const CopperItem* b) const {
  Coord req = r_.minimums.hole_to_hole;
  if (auto c = custom_min("hole_to_hole", a, b, -1)) req = std::max(req, *c);
  return req;
}

Coord RuleEngine::edge_clearance(const CopperItem& a, int layer) const {
  Coord req = r_.minimums.copper_edge_clearance;
  if (auto c = custom_min("edge_clearance", &a, nullptr, layer)) req = *c;
  return req;
}

std::pair<Coord, Coord> RuleEngine::track_width(const CopperItem& t, int layer) const {
  Coord mn = r_.minimums.track_width, mx = 0;
  if (auto c = custom_min("track_width", &t, nullptr, layer)) mn = *c;
  if (auto c = custom_max("track_width", &t, nullptr, layer)) mx = *c;
  return {mn, mx};
}

Coord RuleEngine::via_diameter_min(const CopperItem& v) const {
  Coord req = v.via_type == model::ViaType::Micro ? r_.minimums.microvia_diameter : r_.minimums.via_diameter;
  if (auto c = custom_min("via_diameter", &v, nullptr, -1)) req = *c;
  return req;
}

Coord RuleEngine::annular_width_min(const CopperItem& v) const {
  Coord req = r_.minimums.via_annular_width;
  if (auto c = custom_min("annular_width", &v, nullptr, -1)) req = *c;
  return req;
}

Coord RuleEngine::hole_size_min(const CopperItem* owner) const {
  Coord req = owner && owner->kind == ItemKind::Via && owner->via_type == model::ViaType::Micro ?
                  r_.minimums.microvia_drill : r_.minimums.through_hole_diameter;
  if (auto c = custom_min("hole_size", owner, nullptr, -1)) req = *c;
  return req;
}

}  // namespace tmk::drc
