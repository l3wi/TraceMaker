# Rules, net classes and routing speed

TraceMaker routes on a grid (lattice) and checks every step against the board's rules. Two settings
dominate its speed: the grid pitch, and whether rule checks can use per-net-class caches. Both are set by
the project files, not by the router's options.

## Grid pitch comes from the finest net class

```
pitch = clamp( min over every net class of
               (max(class track, board min track) + max(class clearance, board min clearance)) / 6,
               25 µm, 100 µm )   rounded down to 5 µm
```

Every class defined in `.kicad_pro` counts, whether or not a net uses it. Halving the pitch quadruples the
grid points per layer.

Measured on the StickHub demo (2 layers, fixed 2M work units, one thread): adding one unused 0.1/0.1 mm
class (with board minimums lowered to allow it) moved the pitch from 0.05 to 0.03 mm; the run took 1.7×
as long and routed 98 instead of 100 connections in the same work.

What to do:
- Delete net classes no net uses (KiCad keeps old ones after a schematic change).
- Keep fine classes (BGA breakout, RF) only if nets use them, and assign them by pattern so only those
  nets get them.
- Board minimums should be the fab's limits, not tighter "just in case": they also floor every class.
- `--pitch-um` overrides the automatic pitch; a coarser pitch is faster but can miss tight gaps.

## Custom rules: what keeps the caches

TraceMaker reads the board's `.kicad_dru`. Whether a rule is cheap depends on its constraint and condition:

| Rule | Router behaviour | Speed |
|---|---|---|
| `disallow track/via` with static net/class/type predicates | Pre-evaluated permissions guide search; actual candidate checks remain authoritative | **Cached if class-uniform**; bypassed if predicates differ for nets in one class |
| `physical_hole_clearance` whose condition does not mention `NetName`, `NetClass` or `inDiffPair` | Enforced for every new via | Cached |
| `physical_hole_clearance` that mentions a net or net class | Enforced | **Bypasses caches board-wide** |
| `clearance` (any condition), and any other constraint type | Enforced exactly | **Bypasses caches board-wide** |
| `disallow` with area/courtyard, coordinates, footprint ownership/reference or item dimensions | **Enforced on actual tracks/vias**, including final geometry; residual exact evaluation | **Bypasses caches and cost-to-go fields** |
| `disallow hole/footprint/text` or an unknown disallow type | Unsupported item coverage; preflight **block**. Matching hole predicates conservatively reject new vias | Not a supported routing configuration |
| Unknown property/function, `memberOfGroup`, or unavailable `${Class:...}` selector metadata | Structural diagnostic, even in hidden Boolean branches; preflight **block**. Unknown conditions match conservatively, not as a silently skipped rule | Exact fallback; no KiCad-parity claim |
| Condition that does not parse | Conservative matching; unknown ignored rules cannot waive known earlier restrictions; preflight **block** | **Bypasses caches** |
| Whole `.kicad_dru` file that cannot be read | Routing stops rather than dropping the file's custom constraints | Fix before routing |

Measured on StickHub (fixed work): one `clearance` rule conditioned on a single net made the run 2.15×
slower. The caches go for the whole board, not just the net the rule names.

What to do:
- Put clearances in net classes (a class per clearance need, assigned by pattern) instead of custom
  `clearance` rules.
- Keep-out areas directly express unconditional track/via bans. Supported `insideArea`/courtyard
  conditions are also enforced, and preserve net exemptions and rule precedence that a broad native
  keepout would lose. Do not rewrite them merely to remove a speed finding.
- `tracemaker route BOARD --json route.json` prints project and rule diagnostics to **stderr** and
  records `project_warnings` and `rule_warnings`; the latter matches `RuleEngine::warnings()` exactly.
  `tracemaker drc BOARD` also reports rule diagnostics. Retain them and sign off with KiCad DRC.
- Numbers in conditions are compared as KiCad compares them (exact doubles, units scaled, no rounding).

Supported property names are `NetClass`, `NetName`, `Type`, `Layer`, `Reference`, `Parent.Reference`,
`Pad_Type`, `Size_X`, `Size_Y`, `Width`, `Position_X` and `Position_Y`; bare `L` is the context layer
(not an item property). Calls: `isPlated()`, `existsOnLayer(...)`, `insideArea(...)`,
`intersectsArea(...)`, `enclosedByArea(...)`, `inDiffPair(...)`, `memberOfFootprint(...)`,
`intersectsCourtyard(...)`, `intersectsFrontCourtyard(...)`, `intersectsBackCourtyard(...)`, and
the corresponding `insideCourtyard` / `insideFrontCourtyard` / `insideBackCourtyard` aliases.
Bind item properties/calls to `A.` or `B.`; only `L` is unbound.
`insideArea` and courtyard `inside...` calls mean intersection; `enclosedByArea` means whole
geometry enclosure. Footprint membership tests ownership, not “physically under this part.”
Selectors support reference/library-ID wildcards; component-class metadata is not available.
A selected missing/malformed courtyard or area outline is a capability block (reported by engine DRC
when the binary is available); no placement hull or pad-box substitute is invented. Area target regions
come from their outline, not refilled target copper: fill-sensitive target-zone parity is not claimed.
Front/back courtyard selection swaps on flipped footprints and does not itself constrain copper layers.

Static class-uniform net/class/type disallows retain per-class obstacle caches and cost-to-go fields.
Item/geometry predicates, subtype spans and actual same-class net differences bypass them; prepared
selector polygons and per-net/kind partial verdicts remain cached. A representative net cannot
describe differing net-qualified geometry, candidate widths, coordinates or actual via spans. Candidate checks
are authoritative, including post-search merges, pair legs, escape segments and cleanup. Cache bypass is a speed
trade-off, not missing disallow enforcement. Unsupported existing-item/hole coverage remains a block;
conservative unknown matching cannot establish equivalence with KiCad.

## Net-class patterns are cheap

KiCad 7+ assigns classes by name patterns (`netclass_patterns` in `.kicad_pro`). TraceMaker resolves each
net's class once when routing starts. Earlier versions matched patterns at every grid step: on a private
4-layer board that was about 80 % of the run time, and resolving once took a 20M-work route from about
140 s to 10 s (5M work: 32 s → 4 s) with identical output. On StickHub, two catch-all patterns now cost
about 15 %. Use patterns freely; that is the intended way to give groups of nets their own class.

## Via sizes

The via the router places for a class is

```
drill    = max(class via drill, board min through-hole diameter)
diameter = max(class via diameter, board min via diameter, drill + 2 × board min annular width)
```

When the class via does not fit, it falls back to a smaller "neck-down" via at the board minimums
(drill never below 0.2 mm). KiCad accepts it (it checks board minimums, not class sizes). If the class via
size is a hard requirement, raise the board minimums to it.

Blind, buried and micro vias are used only with `--blind-vias` and only when the board settings allow them.
