# Common symbols (pins in symbol coordinates, R0; y grows downward)

Outward direction: `<` left, `>` right, `^` up, `v` down. Use `$XS sym NAME` for anything else,
and `$XS find PATTERN` to search.

## IHP sg13g2 primitives (`sg13g2_pr/...`)
| symbol | pins | key params (template) |
|---|---|---|
| sg13g2_pr/sg13_lv_nmos | D@20,-30^ G@-20,0< S@20,30v B@20,0> | l=0.13u w=0.15u ng=1 m=1 |
| sg13g2_pr/sg13_lv_pmos | S@20,-30^ G@-20,0< D@20,30v B@20,0> | l=0.13u w=0.15u ng=1 m=1 |
| sg13g2_pr/sg13_hv_nmos / sg13_hv_pmos | same as lv | l=0.45u/0.4u w=0.3u |
| sg13g2_pr/rppd, rsil, rhigh | P@0,-30^ M@0,30v | w l b m (value is computed) |
| sg13g2_pr/cap_cmim | c0@0,-30^ c1@0,30v | w=7e-6 l=7e-6 m=1 |
| sg13g2_pr/npn13G2 | C@20,-30 B@-20,0 E@20,30 S@20,0 | Nx=1 |
| sg13g2_pr/pnpMPA | emitter@20,-30 base@-20,0 collector@20,30 | w l m |
| sg13g2_pr/ntap1, ptap1 | P@0,-30 M@0,30 | w l |
| sg13g2_pr/dantenna, dpantenna | d1@0,-30 d0@0,30 | w l |
| sg13g2_stdcell cells (bare name, e.g. sg13g2_inv_1) | inv: A Y; nand2: A B Y; dfrbp: CLK D RESET_B Q Q_N | |

MOS params: total width = w (per finger = w/ng); m = multiplier. Models need
`.lib cornerMOSlv.lib mos_tt` (or `cornerMOShv.lib`) in a simulator_commands block.

## xschem devices (bare names)
| symbol | pins | template |
|---|---|---|
| vsource | p@0,-30^ m@0,30v | value=3 (e.g. "dc 0.6 ac 1", "pulse(0 1.2 0 10p 10p 1n 2n)") |
| isource | p@0,-30^ m@0,30v | value=1m (current flows p→m through the source) |
| res / capa / ind | P,M / p,m / p,m @0,∓30 | value= |
| vcvs / vccs | p@0,-30 m@0,30 cp@-40,-20 cm@-40,20 | value= gain |
| diode | p@0,-30 m@0,30 | model= |
| gnd / vdd | p@0,0 (body down / up) | lab=0 / lab=VDD |
| lab_pin / lab_wire | p@0,0 | lab= |
| ipin / opin / iopin | p@0,0 | lab= (subcircuit ports) |
| noconn | p@0,0 | marks an intentionally open pin |
| simulator_commands_shown | none | value="..." simulator=ngspice |
| code_shown | none | value="..." (netlist text) |
| title, launcher | none | decorative/tcl buttons |

## Symbol file anatomy (for reading raw .sym/.sch if ever needed)
- `K {type=subcircuit format="@name @pinlist @symname" template="name=x1"}`: global attributes
- `B 5 x1 y1 x2 y2 {name=A dir=in}`: pin box (pin location = box centre)
- `L layer x1 y1 x2 y2 {}` line, `T {text} x y rot flip xs ys {}` text, `P`, `A` polygon/arc
- In .sch: `N x1 y1 x2 y2 {lab=NET}` wire, `C {sym} x y rot flip {props}` instance
- Transform: flip mirrors x first, then rot r turns 90°·r so that (x,y)→(-y,x) for r=1.
