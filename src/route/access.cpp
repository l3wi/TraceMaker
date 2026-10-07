// SPDX-License-Identifier: GPL-3.0-or-later
#include "route/escape.hpp"
#include "route/obstacles.hpp"
#include <algorithm>
#include <array>
#include <bit>
#include <cmath>
#include <limits>
#include <map>
#include <queue>
#include <set>
#include <tuple>

namespace tmk::route {
Coord access_width_floor(const model::DesignRules& r) {
  Coord declared = std::numeric_limits<Coord>::max();
  for (const auto& c : r.classes) if (c.track_width > 0) declared = std::min(declared, c.track_width);
  if (declared == std::numeric_limits<Coord>::max()) declared = r.minimums.track_width;
  return r.minimums.track_width > 0 ? r.minimums.track_width : declared;
}
namespace {
Coord length(Point a, Point b) {
  return static_cast<Coord>(std::llround(std::hypot(static_cast<double>(b.x-a.x), static_cast<double>(b.y-a.y))));
}
// Circumcentres recover the centre of a rounded clearance pocket from its boundary, not from a grid phase.
// Candidate construction only: integer exact Obstacles checks remain authoritative (Yan & Wong, ICCAD 2010).
bool circle_centre(Point a, Point b, Point c, Point& out) {
  const long double bx=static_cast<long double>(b.x-a.x), by=static_cast<long double>(b.y-a.y),
                    cx=static_cast<long double>(c.x-a.x), cy=static_cast<long double>(c.y-a.y);
  const long double d=2*(bx*cy-by*cx);
  if (std::fabs(d)<1) return false;
  const long double bb=bx*bx+by*by, cc=cx*cx+cy*cy;
  const long double x=(bb*cy-cc*by)/d, y=(bx*cc-cx*bb)/d;
  const long double rad=std::hypot(x,y);
  if (rad<100'000 || rad>1'000'000) return false;
  out={a.x+static_cast<Coord>(std::llround(x)),a.y+static_cast<Coord>(std::llround(y))};
  return true;
}
}
AccessSearchResult generate_access_paths(const model::Board& b, const model::DesignRules& r, Obstacles& obs,
                                        int pad, const AccessSearchOptions& o) {
  AccessSearchResult result;
  auto charge = [&](long& category) {
    if (o.work_budget > 0 && result.work >= o.work_budget) return false;
    ++result.work; ++category; return true;
  };
  auto spend = [&]() { return charge(result.generation_work); };
  bool stopped = false;
  const std::function<bool()> target_check = [&]() {
    if (!charge(result.check_work)) { stopped = true; return false; }
    return true;
  };
  const auto& p=b.pads.at(static_cast<std::size_t>(pad));
  const auto& nc=r.class_for(b.nets.at(static_cast<std::size_t>(p.net)).name);
  const Coord normal=std::max(nc.track_width,r.minimums.track_width);
  const Coord width=o.width>0 ? o.width : normal;
  const Coord floor=access_width_floor(r);
  if (floor<=0 || o.pitch<=0 || o.radius<=0) return result;
  if (width < floor) { result.exhausted = true; return result; }
  const int nl=b.copper_count();
  const auto normal_via=class_via(r,nc), small_via=neck_down_via(r,nc);
  const ClassVia via_choices[]{normal_via, small_via};
  const bool can_blind = nl > 2 && o.routing.blind_vias && r.minimums.allow_blind_buried_vias;
  bool via_permitted = false;
  if (o.routing.allow_vias || can_blind) {
    if (!charge(result.check_work)) return result;
    via_permitted = obs.rules().via_allowed(p.net);
  }
  bool source_blocked = true;
  for (int l = 0; l < nl; ++l) if (p.copper & model::layer_bit(l)) {
    if (!charge(result.check_work)) return result;
    if (!obs.rules().track_allowed(p.net, l)) continue;
    if (!charge(result.check_work)) return result;
    if (!obs.track_source_blocked(p.pos, l, floor / 2)) source_blocked = false;
  }
  if (source_blocked) {
    bool source_via = false;
    if (via_permitted) for (std::size_t vi = 0; vi < 2 && !source_via; ++vi) {
      const auto via = via_choices[vi];
      if (vi && via.diameter == normal_via.diameter && via.drill == normal_via.drill) continue;
      if (o.routing.allow_vias) {
        if (!charge(result.check_work)) return result;
        source_via = obs.via_state(p.pos, via.diameter, via.drill, p.net, 0, true) != 2;
      }
      if (can_blind && !source_via) for (int from = 0; from < nl && !source_via; ++from) {
        if (!(p.copper & model::layer_bit(from))) continue;
        for (int to = 0; to < nl && !source_via; ++to) {
          if (!charge(result.check_work)) return result;
          if (to == from || (std::min(from, to) == 0 && std::max(from, to) == nl - 1) ||
              !obs.rules().track_allowed(p.net, to)) continue;
          if (!charge(result.check_work)) return result;
          source_via = obs.via_state_span(p.pos, via.diameter, via.drill, p.net, 0, true, nullptr,
              std::min(from, to), std::max(from, to)) != 2;
        }
      }
    }
    if (!source_via) { result.exhausted = true; return result; }
  }
  const Coord clearance=std::max(nc.clearance,r.minimums.clearance);
  std::set<std::pair<Coord,Coord>> coords;
  auto add=[&](Point q) {
    if (std::llabs(q.x-p.pos.x)<=o.radius && std::llabs(q.y-p.pos.y)<=o.radius) coords.emplace(q.x,q.y);
  };
  add(p.pos);
  const auto [hx,hy]=pad_half_extents(p);
  // Pad-aligned rays and interstitial sites survive arbitrary package translations and flips.
  constexpr int dx[]={1,1,0,-1,-1,-1,0,1},dy[]={0,1,1,1,0,-1,-1,-1};
  for (int d=0;d<8;++d) for (Coord k=100'000;k<=o.radius;k+=100'000) {
    if (!spend()) return result;
    add({p.pos.x+dx[d]*k,p.pos.y+dy[d]*k});
  }
  for (const auto& other:b.pads) {
    if (!spend()) return result;
    if (length(other.pos,p.pos)>2*o.radius) continue;
    add(other.pos);
    add({(p.pos.x+other.pos.x)/2,(p.pos.y+other.pos.y)/2});
    const auto [ox,oy]=pad_half_extents(other);
    for (int d=0;d<8;++d) {
      if (!spend()) return result;
      add({other.pos.x+dx[d]*(ox+clearance+floor/2+1),other.pos.y+dy[d]*(oy+clearance+floor/2+1)});
    }
  }
  add({p.pos.x-hx-clearance-floor/2-1,p.pos.y});
  add({p.pos.x+hx+clearance+floor/2+1,p.pos.y});
  add({p.pos.x,p.pos.y-hy-clearance-floor/2-1});
  add({p.pos.x,p.pos.y+hy+clearance+floor/2+1});
  std::vector<Point> vertices;
  vertices.reserve(obs.copper().items.size() * 4);
  // Local boundary portals: both sides of offsets, bisectors, channel midpoints, and rounded pocket centres.
  // The reference scans all items; the accelerated path rejects only disjoint bounding boxes.
  for (const auto& item:obs.copper().items) {
    if (!spend()) return result;
    if (item.removed || item.owner>=0 || item.net==p.net) continue;
    if (!o.reference && (item.box.x1<p.pos.x-o.radius-1'000'000 || item.box.x0>p.pos.x+o.radius+1'000'000 ||
                        item.box.y1<p.pos.y-o.radius-1'000'000 || item.box.y0>p.pos.y+o.radius+1'000'000)) continue;
    for (const auto& s:item.shapes) {
      const std::size_t n=s.pts.size();
      for (std::size_t i=0;i<n;++i) {
        if (!spend()) return result;
        const Point a=s.pts[i];
        if (std::llabs(a.x-p.pos.x)>o.radius+1'000'000 || std::llabs(a.y-p.pos.y)>o.radius+1'000'000) continue;
        if (n <= 2) {
          for (int d = 0; d < 8; ++d) {
            if (!spend()) return result;
            const Coord off = s.r + clearance + floor / 2 + 1;
            add({a.x + dx[d] * off, a.y + dy[d] * off});
          }
          vertices.push_back(a);
          continue;
        }
        const Point before=s.pts[(i+n-1)%n],next=s.pts[(i+1)%n];
        Point centre;
        if (n>=3 && length(before,a)<200'000 && length(a,next)<200'000 && circle_centre(before,a,next,centre)) {
          add(centre);
          // Polygon integer rounding can perturb circumcentres; nearby nanometre candidates are still exact checked.
          for (Coord snap : {Coord{1'000},Coord{10'000}}) {
            if (!spend()) return result;
            add({static_cast<Coord>(std::llround(static_cast<double>(centre.x)/static_cast<double>(snap)))*snap,
                 static_cast<Coord>(std::llround(static_cast<double>(centre.y)/static_cast<double>(snap)))*snap});
          }
          continue;
        }
        const long double l1=static_cast<long double>(length(before,a)),l2=static_cast<long double>(length(a,next));
        if (l1==0 || l2==0) continue;
        const long double nx1=-static_cast<long double>(a.y-before.y)/l1,ny1=static_cast<long double>(a.x-before.x)/l1;
        const long double nx2=-static_cast<long double>(next.y-a.y)/l2,ny2=static_cast<long double>(next.x-a.x)/l2;
        // Straight runs and finely sampled small curves do not each need a visibility-graph corner.
        // Rounded via pockets were handled above; retain the actual turning boundary features here.
        if (nx1 * nx2 + ny1 * ny2 > 0.99L) continue;
        vertices.push_back(a);
        const long double denom=1+nx1*nx2+ny1*ny2;
        for (Coord w : {floor,width}) {
          const Coord off=clearance+w/2+s.r+1;
          for (int sign : {-1,1}) {
            if (!spend()) return result;
            const long double offset = static_cast<long double>(sign * off);
            add({a.x+static_cast<Coord>(std::llround(offset*nx2)),a.y+static_cast<Coord>(std::llround(offset*ny2))});
            if (denom>0.05L) add({a.x+static_cast<Coord>(std::llround(offset*(nx1+nx2)/denom)),
                                 a.y+static_cast<Coord>(std::llround(offset*(ny1+ny2)/denom))});
          }
        }
      }
    }
  }
  // Adjacent boundary samples define channel centres. Pair in both coordinate orders rather than
  // forming quadratic combinations of the many tessellation vertices in a filled-zone boundary.
  std::sort(vertices.begin(),vertices.end(),[](Point a,Point c){return std::tie(a.x,a.y)<std::tie(c.x,c.y);});
  vertices.erase(std::unique(vertices.begin(),vertices.end()),vertices.end());
  for (int axis = 0; axis < 2; ++axis) {
    if (axis) std::sort(vertices.begin(), vertices.end(), [](Point a, Point c) {
      return std::tie(a.y, a.x) < std::tie(c.y, c.x);
    });
    for (std::size_t i = 0; i < vertices.size(); ++i)
      for (std::size_t j = i + 1; j < std::min(vertices.size(), i + 9); ++j) {
        if (!spend()) return result;
        if (length(vertices[i], vertices[j]) > 1'000'000) continue;
        add({(vertices[i].x + vertices[j].x) / 2, (vertices[i].y + vertices[j].y) / 2});
      }
  }
  std::vector<Point> points;
  std::vector<model::LayerMask> layers;
  points.reserve(coords.size()); layers.reserve(coords.size());
  for (const auto& [x,y]:coords) {
    if (!spend()) return result;
    Point q{x,y}; model::LayerMask mask=0;
    for (int l=0;l<nl;++l) {
      if (!charge(result.check_work)) return result;
      if (!obs.rules().track_allowed(p.net,l)) continue;
      if (!charge(result.check_work)) return result;
      if (obs.disk_state(q,l,floor/2,p.net,0,true)!=2) mask|=model::layer_bit(l);
    }
    if (mask || q==p.pos) {points.push_back(q);layers.push_back(mask);}
  }
  const std::size_t count=points.size();
  if (o.record_candidates) {
    result.candidates.reserve(points.size());
    for (std::size_t i = 0; i < points.size(); ++i) {
      if (!spend()) return result;
      result.candidates.emplace_back(points[i], layers[i]);
    }
  }
  if (!count) {result.exhausted=true;return result;}
  const auto start_it=std::find(points.begin(),points.end(),p.pos);
  const std::size_t start=static_cast<std::size_t>(start_it-points.begin());
  const auto inf=std::numeric_limits<std::int64_t>::max()/4;
  struct Parent {std::size_t state=SIZE_MAX;Coord width=0;int v0=-1,v1=-1;Coord diameter=0,drill=0;};
  std::vector<std::int64_t> cost(count*static_cast<std::size_t>(nl),inf);
  std::vector<Parent> parent(cost.size());
  using Entry=std::pair<std::int64_t,std::size_t>;
  std::priority_queue<Entry,std::vector<Entry>,std::greater<>> queue;
  for (int l=0;l<nl;++l) if (p.copper&model::layer_bit(l)) {
    const std::size_t s=static_cast<std::size_t>(l)*count+start;cost[s]=0;queue.emplace(0,s);
  }
  drc::CopperItem track;
  track.kind = drc::ItemKind::Track;
  track.net = p.net;
  track.shapes.push_back(geom::Shape::segment(p.pos, p.pos, 0));
  auto edge_ok = [&](Point a, Point c, int layer, Coord w) {
    if (!charge(result.check_work)) { stopped = true; return false; }
    return obs.segment_state(a, c, layer, w, p.net, true) != 2;
  };
  // CSR buckets restrict neighbor enumeration without changing the nearest portals or their tie-breaks.
  constexpr Coord bucket_pitch = 250'000;
  const Point bucket_origin{p.pos.x - o.radius, p.pos.y - o.radius};
  const std::size_t side = static_cast<std::size_t>(2 * o.radius / bucket_pitch) + 1;
  const std::size_t buckets = side * side;
  std::vector<std::size_t> offsets, bucket_items;
  auto bucket_of = [&](Point point) {
    return static_cast<std::size_t>((point.y - bucket_origin.y) / bucket_pitch) * side +
        static_cast<std::size_t>((point.x - bucket_origin.x) / bucket_pitch);
  };
  if (!o.reference) {
    offsets.assign(buckets * static_cast<std::size_t>(nl) + 1, 0);
    for (std::size_t i = 0; i < count; ++i) {
      if (!spend()) return result;
      auto mask = layers[i];
      while (mask) {
        if (!spend()) return result;
        const auto layer = static_cast<std::size_t>(std::countr_zero(mask));
        ++offsets[layer * buckets + bucket_of(points[i]) + 1];
        mask &= mask - 1;
      }
    }
    for (std::size_t i = 1; i < offsets.size(); ++i) {
      if (!spend()) return result;
      offsets[i] += offsets[i - 1];
    }
    auto cursor = offsets;
    bucket_items.resize(offsets.back());
    for (std::size_t i = 0; i < count; ++i) {
      if (!spend()) return result;
      auto mask = layers[i];
      while (mask) {
        if (!spend()) return result;
        const auto layer = static_cast<std::size_t>(std::countr_zero(mask));
        bucket_items[cursor[layer * buckets + bucket_of(points[i])]++] = i;
        mask &= mask - 1;
      }
    }
  }
  std::vector<std::array<std::int8_t, 2>> via_cache;
  if (!o.reference) via_cache.assign(count, std::array<std::int8_t, 2>{-1, -1});
  auto make_path=[&](std::size_t s) {
    AccessPath path;path.layer=static_cast<int>(s/count);path.end=points[s%count];path.cost=cost[s];
    path.steps.reserve(32); path.vias.reserve(4);
    while (parent[s].state!=SIZE_MAX) {
      if (!charge(result.expansion_work)) { stopped = true; return AccessPath{}; }
      const auto& par=parent[s];const auto prev=par.state;
      if (par.v0>=0) path.vias.push_back({points[s%count],par.diameter,par.drill,par.v0,par.v1,
          par.v0==0 && par.v1==nl-1 ? model::ViaType::Through : model::ViaType::Blind,p.net,false,sexpr::kNoNode});
      else path.steps.push_back({points[prev%count],points[s%count],static_cast<int>(s/count),par.width});
      s=prev;
    }
    std::reverse(path.steps.begin(),path.steps.end());std::reverse(path.vias.begin(),path.vias.end());return path;
  };
  while (!queue.empty()) {
    if (!charge(result.expansion_work)) return result;
    const auto [g,s]=queue.top();queue.pop();if (g!=cost[s]) continue;
    const int l=static_cast<int>(s/count);const std::size_t i=s%count;const Point q=points[i];
    if (o.target && !charge(result.check_work)) return result;
    const auto gx=static_cast<Coord>(std::llround(static_cast<double>(q.x-o.origin.x)/static_cast<double>(o.pitch)));
    const auto gy=static_cast<Coord>(std::llround(static_cast<double>(q.y-o.origin.y)/static_cast<double>(o.pitch)));
    const Point join{o.origin.x+gx*o.pitch,o.origin.y+gy*o.pitch};
    auto goal=o.target ? o.target(q,l,target_check) :
        (length(q,p.pos)>=o.radius/2 || !(p.copper&model::layer_bit(l)) ? AccessGoal::Endpoint : AccessGoal::None);
    bool goal_at_join = false;
    if (goal != AccessGoal::Connected && o.target && o.lattice_target && !(q == join)) {
      goal = o.target(join, l, target_check);
      goal_at_join = goal != AccessGoal::None;
    }
    const bool snap = !o.target || goal_at_join || (o.lattice_target && goal == AccessGoal::Endpoint);
    bool joined = goal != AccessGoal::None && (!snap ||
        (std::llabs(join.x-p.pos.x)<=o.radius && std::llabs(join.y-p.pos.y)<=o.radius && edge_ok(q, join, l, width)));
    if (joined && goal != AccessGoal::Connected) for (const auto& path : result.paths) {
      if (!charge(result.neighbor_work)) return result;
      if (path.layer == l && path.end == (snap ? join : q)) { joined = false; break; }
    }
    if (stopped) return result;
    if (joined) {
      auto path=make_path(s);
      path.target_connected = goal == AccessGoal::Connected;
      if (stopped) return result;
      if (snap) {
        if (!(q==join)) path.steps.push_back({q,join,l,width});
        path.end=join;path.cost+=length(q,join);
      }
      result.paths.push_back(std::move(path));
      if (goal == AccessGoal::Connected || static_cast<int>(result.paths.size())>=o.max_paths) return result;
    }
    auto relax = [&](std::size_t j, Coord len) {
      const std::size_t t=static_cast<std::size_t>(l)*count+j;
      if (g+len>=cost[t]) return;
      Coord w=width;
      if (!edge_ok(q,points[j],l,w)) {
        if (stopped) return;
        if (!charge(result.check_work)) { stopped = true; return; }
        track.width = width; track.pos = q; track.layers = model::layer_bit(l);
        auto& shape = track.shapes.front();
        shape.pts[0] = q; shape.pts[1] = points[j]; shape.r = width / 2;
        shape.update_box(); track.box = shape.box;
        const Coord edge_floor = std::max(floor, obs.rules().track_width(track, l).first);
        if (edge_floor >= w || !edge_ok(q,points[j],l,edge_floor)) return;
        Coord lo=edge_floor,hi=w;
        while (hi-lo>1) {
          const Coord mid=lo+(hi-lo)/2;
          if (!edge_ok(q,points[j],l,mid)) hi=mid; else lo=mid;
          if (stopped) return;
        }
        w=lo;
      }
      cost[t]=g+len;parent[t]={s,w,-1,-1};queue.emplace(cost[t],t);
      if (stopped) return;
    };
    if (i == start) {
      for (std::size_t j = 0; j < count; ++j) {
        if (!charge(result.neighbor_work)) return result;
        if (i == j || !(layers[j] & model::layer_bit(l))) continue;
        relax(j, length(q, points[j]));
        if (stopped) return result;
      }
    } else {
      // A finite local visibility graph, not a dense graph over every fill tessellation sample.
      // Preserve sixteen nearest portals in each octant; the source retains every direct exit.
      std::array<std::array<Entry, 16>, 8> nearest;
      for (auto& octant : nearest) octant.fill({inf, SIZE_MAX});
      auto consider = [&](std::size_t j) {
        if (!charge(result.neighbor_work)) { stopped = true; return; }
        if (i == j || !(layers[j] & model::layer_bit(l))) return;
        const Point delta = points[j] - q;
        const Coord len = length(q, points[j]);
        if (len > 1'500'000) return;
        const bool horizontal = std::llabs(delta.x) >= std::llabs(delta.y);
        const std::size_t sector = delta.x >= 0 ? (delta.y >= 0 ? (horizontal ? 0U : 1U) : (horizontal ? 7U : 6U)) :
            (delta.y >= 0 ? (horizontal ? 3U : 2U) : (horizontal ? 4U : 5U));
        auto& octant = nearest[sector];
        const Entry candidate{len, j};
        if (candidate >= octant.back()) return;
        const auto at = std::lower_bound(octant.begin(), octant.end(), candidate);
        std::move_backward(at, octant.end() - 1, octant.end());
        *at = candidate;
      };
      if (o.reference) {
        for (std::size_t j = 0; j < count; ++j) { consider(j); if (stopped) return result; }
      } else {
        const auto cell = [&](Coord value, Coord origin) {
          return std::min(side - 1, static_cast<std::size_t>(std::max(Coord{0}, value - origin) / bucket_pitch));
        };
        const std::size_t x0 = cell(q.x - 1'500'000, bucket_origin.x), x1 = cell(q.x + 1'500'000, bucket_origin.x);
        const std::size_t y0 = cell(q.y - 1'500'000, bucket_origin.y), y1 = cell(q.y + 1'500'000, bucket_origin.y);
        for (std::size_t y = y0; y <= y1; ++y) for (std::size_t x = x0; x <= x1; ++x) {
          if (!charge(result.neighbor_work)) return result;
          const std::size_t bucket = static_cast<std::size_t>(l) * buckets + y * side + x;
          for (std::size_t at = offsets[bucket]; at < offsets[bucket + 1]; ++at) {
            consider(bucket_items[at]); if (stopped) return result;
          }
        }
      }
      for (const auto& octant : nearest) for (const auto& [len, j] : octant) {
        if (j == SIZE_MAX) break;
        if (!charge(result.neighbor_work)) return result;
        relax(j, len);
        if (stopped) return result;
      }
    }
    if (via_permitted && (layers[i] & ~model::layer_bit(l))) for (std::size_t vi = 0; vi < 2; ++vi) {
      const auto via = via_choices[vi];
      if (!charge(result.neighbor_work)) return result;
      if (vi && via.diameter == normal_via.diameter && via.drill == normal_via.drill) continue;
      bool through = false;
      if (o.routing.allow_vias) {
        if (!charge(result.check_work)) return result;
        if (o.reference) through = obs.via_state(q, via.diameter, via.drill, p.net, 0, true) != 2;
        else {
          auto& cached = via_cache[i][vi];
          if (cached < 0)
            cached = static_cast<std::int8_t>(obs.via_state(q, via.diameter, via.drill, p.net, 0, true) != 2);
          through = cached != 0;
        }
      }
      for (int to=0;to<nl;++to) {
        if (!charge(result.neighbor_work)) return result;
        if (to==l || !(layers[i]&model::layer_bit(to))) continue;
        const int a=std::min(l,to),c=std::max(l,to);
        if (!through) {
          if (!can_blind || (a == 0 && c == nl - 1)) continue;
          if (!charge(result.check_work)) return result;
          if (obs.via_state_span(q,via.diameter,via.drill,p.net,0,true,nullptr,a,c)==2) continue;
        }
        const std::size_t t=static_cast<std::size_t>(to)*count+i;
        const auto vg=g+static_cast<Coord>(o.routing.via_cost_mm*1'000'000);
        if (vg>=cost[t]) continue;
        cost[t]=vg;parent[t]={s,0,through?0:a,through?nl-1:c,via.diameter,via.drill};queue.emplace(vg,t);
      }
    }
  }
  result.exhausted=true;return result;
}
} // namespace tmk::route
