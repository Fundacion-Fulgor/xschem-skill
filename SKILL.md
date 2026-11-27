---
name: xschem
description: Read, evaluate, draw and edit xschem schematics (.sch) and symbols (.sym) with low context overhead and verified connectivity. Use whenever a task involves an xschem file - summarizing or reviewing a circuit, finding connectivity bugs, creating a schematic/testbench/symbol, changing device parameters, rewiring, generating a netlist, or rendering a picture of a schematic (IHP sg13g2 / iic-osic-tools environment).
---

# xschem skill

All work goes through one tool: `XS=~/.claude/skills/xschem/xs` (stdlib Python, runs on the host in
~50 ms). Only `netlist`, `render` and `xcheck` start xschem, inside distrobox `iic-osic-tools2`,
always headless (`-x`) with a timeout.

**Never launch the xschem GUI** (no `xschem file.sch` without `-x`): windows pop up on the user's
desktop and they have to close them by hand. Don't hand-edit .sch/.sym text either; use `xs edit`.
It keeps untouched lines byte-identical and reports the connectivity result.

## Commands

```
$XS read F.sch            # ports, instances (sym @pos props | pin=net), blocks, warnings
$XS read F.sch -n         # + net-centric list     --net 'OUT,VB*' nets with wire geometry
$XS read F.sch -g -i 'M*' # + pin coords & outward direction (<>^v) for matching instances
$XS read F.sym            # symbol pins/format/template (+ check against sibling .sch ports)
$XS sym sg13_lv_nmos      # library symbol info by (loose) name;  $XS find nmos  # search libs
$XS check F.sch ...       # warnings only
$XS edit F.sch <<'EOF'    # apply edit DSL (creates file if missing); atomic: any error -> nothing saved
...
EOF
$XS edit F.sch -e 'set M1 w=2u; set M2 w=2u' [--dry]
$XS mksym F.sch [--force] [--l A,B --r C --u VDD --d VSS]   # box symbol from ipin/opin/iopin
$XS netlist F.sch [--flat|--lvs] [-o out.spice]           # via xschem, prints netlist + xschem warnings
$XS render F.sch|F.sym [-o out.png] [--dark]  # PNG via xschem -> then Read the PNG to look at it
$XS xcheck F.sch          # verify xs connectivity == xschem's own connectivity, pin by pin
$XS env [--refresh]       # rc file / library path in use (refresh after changing xschemrc)
```

Net names in output: label/port names, `#nK` = unnamed net, `NC` = pin touches nothing.

## Edit DSL (one command per line or `;`-separated; `#` comments; "quotes" for spaces/newlines)

Points: `x,y` | `INST.PIN` | `INST.PIN+dx,dy` | mixed `140,M3.G` (x from number, y from pin).
In `wire`/`line` chains, after the first point also: `x=V` `y=V` (V may be a pin ref: `y=M2.S`), `dx=V` `dy=V`.

| command | effect |
|---|---|
| `place NAME\|auto SYM AT [R0-3] [F] [anchor=PIN] [k=v..]` | instantiate; template props are copied; `anchor=D` puts pin D on AT |
| `copy SRC NEW AT [R..] [F] [anchor=PIN] [k=v..]` | duplicate with props |
| `move INST AT\|by=dx,dy [R..] [F] [anchor=PIN] [norubber]` | wires and labels attached to its pins follow |
| `set INST[,INST\|glob] k=v ..` (`k=` deletes) · `rename OLD NEW` · `sym INST NEWSYM` | properties / swap symbol |
| `rm NAME\|glob ..` · `rmwire P [P2]` · `rmnet NET [keeplabels]` | delete |
| `wire P1 P2 [P3..] [auto\|hv\|vh\|hvh\|vhv\|direct]` | orthogonal wires; `auto` (default) picks the L/Z that leaves pins outward and avoids pins, wire ends, overlaps and crossings |
| `label REF NET [len=L] [dir=l\|r\|u\|d] [sym=lab_wire.sym]` | lab_pin auto-oriented away from the pin; `len` draws a stub first |
| `net NET REF REF ..` | label several pins with the same net |
| `port REF NET in\|out\|io [len=L] [dir=]` | ipin/opin/iopin (subcircuit ports) |
| `gnd REF [NET=GND]` · `vdd REF [NET=VDD]` | gnd.sym / vdd.sym |
| `text "str" AT [size=0.4]` | annotation |
| symbols: `pin NAME AT dir=in\|out\|inout side=l\|r\|u\|d [len=20]` · `rmpin` · `renpin OLD NEW` · `setpin NAME k=v` · `movepin NAME AT` · `setk k=v` (K block: type/format/template) · `line` · `box` · `poly` | symbol editing |

Multi-line property values: `place s1 simulator_commands_shown -400,-300 value=".lib cornerMOSlv.lib mos_tt
.control
op
.endc"`.

## Workflows

**Evaluate an existing schematic:** `read` (plus `-n` for topology). Walk the circuit from the
pin=net map: look for floating gates, missing bulk ties, wrong mirror orientation, swapped D/S, and
port and direction mismatches. Treat every `warnings` line as a lead. Use `render` + Read only when
layout or readability matters. For hierarchy, read the subcell .sch files (`x1 RES` → `RES.sch`).

**Draw a new schematic:**
1. `$XS sym` the devices you need (see `reference.md` for common IHP and xschem symbols).
2. One `edit` heredoc: place devices with `anchor=` so pins line up, `wire` pin to pin, then add `port`/`label`/`gnd`/`vdd`.
3. Read the edit report. Every device's pin=net must match the intended netlist, and `new warnings` must be `none` (or understood).
4. `render` → Read the PNG → fix overlaps or ugly routing with `move` (rubber-banding) or `rmwire` + `wire`.
5. `xcheck` and/or `netlist` as final proof that xschem agrees.

**Modify:** use `set` for parameters, and `move`/`rmwire`/`wire`/`label` for topology. The edit report
lists `net changes` for every device pin whose net changed. If anything changed that you didn't
intend, fix it before moving on.

**Symbol for a subcircuit:** `mksym cell.sch` (inputs left, outputs right, VDD-like pins top,
VSS-like pins bottom, other inouts right; use `--l/--r/--u/--d` to override). `read cell.sym`
reports any mismatch with the schematic's ports. The symbol's directory must be in the xschem
library path; otherwise `check` warns "not in the xschem library path".

## Connectivity rules (same as xschem; verified with xcheck on 300+ real schematics)

- A wire endpoint connects to any pin or wire endpoint at that point, and to a **wire interior** (T-junction, drawn with a dot).
- **Crossing wire interiors do NOT connect.** To connect a crossing, split it into a T.
- An instance pin lying on a wire's **interior connects** (`check` warns, because it's usually accidental).
- Pins at the same point connect. Same-named pins of one symbol are internally shorted (short.sym).
- Labels (lab_pin, lab_wire, gnd, vdd, ipin/opin/iopin) with the same `lab` are one net. Two names on one net = `SHORT` warning.
- `spice_ignore=true` instances are removed from connectivity; `spice_ignore=short` shorts their pins.
- Bus names like `D[3:0]` are kept as strings (xschem expands them).
- xschem finds symbols only through `XSCHEM_LIBRARY_PATH`, **not** the schematic's own directory. The rc used is the nearest `xschemrc` walking up from the file (override with `XS_RC=path` or `XS_RC=default`).

## Drawing conventions for neat schematics

- Grid: keep everything on multiples of 10 (pins of standard symbols land on 10/20 grid when the origin does).
- Signal flow left→right; inputs left, outputs right; VDD/PMOS top, VSS/NMOS bottom.
- IHP MOS symbols: origin at the gate/channel; G at (-20,0); D/S at (20,∓30) (nmos D on top, pmos S on top); B at (20,0). Parameter text occupies about x=30..70 to the right, so:
  - Space side-by-side MOS (one flipped with `F`) ≥ 240 apart, and stacked devices ≥ 120 apart.
  - Bulk labels: `label M1.B VSS len=60` gets the label clear of the parameter text. Alternatively, tie B to S with a wire when they are the same net.
- Differential pairs and mirrors: place the right device with `F` so gates face outward or inward symmetrically; `anchor=D` against `M1.D+0,-120` aligns stacks exactly.
- Prefer short direct wires. For long or global connections (supplies, bias, clocks), use labels instead of long wires. Never route a wire through a pin it shouldn't touch.
- Ports: `port M1.G VINP in len=40`. A port or label on a wire's interior is fine.
- Testbenches: sources from `devices` (`vsource`, `isource`), ground with `gnd` (net `GND`; ngspice treats gnd as node 0). For simulator commands, use `simulator_commands_shown` with a multi-line `value`. IHP models: `.lib cornerMOSlv.lib mos_tt` (see the PDK's sg13g2_tests for more).

## Notes
- Caches: `~/.cache/xschem-skill/` (library path per rc; mirror of the container-only symbol libraries under `/foss`). If symbols seem missing after a tool or PDK change, run `$XS env --refresh`.
- `render` writes to `~/.cache/xschem-skill/render/NAME.png` unless `-o` is given. Light theme by default.
- Duplicate instance names, `.sch` instantiated directly (LCC) and `tcleval(...)` attributes are only partially analysed; `check` says so.
