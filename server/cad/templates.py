# Adapted from LoominPhysicsSandbox (Preet37, pre-existing project) lib/specToCad.ts and app/api/geometry-render/route.ts (classifyTopic + topic templates), ported to Python for Twin.
"""Known-model library: hand-written OpenSCAD templates per object category.

Spec-grounded templates (car, aircraft, racket) take validated spec-sheet dimensions
(mm) and scale them to model units; the rest are fixed parametric-free templates.
All of this is fixed source shipped with Twin: the only model-derived inputs are
numbers (from a validated spec sheet) and the topic, which lands in a // comment
after sanitising. Loomin's cube-plus-sphere "generic" template is deliberately not
ported: an unknown object goes to the researched plan (cad.plan) or fails loudly.

Twin changes: classify_topic uses word boundaries (Loomin's /van/ matched
"relevant", /bus/ "business", /iss/ "mission", /arch/ "search").
"""
from __future__ import annotations

import re
from typing import Callable

from .spec import SpecSheet

_CATEGORIES: list[tuple[str, str]] = [
    ("car", r"f1|formula[- ]?1|racecars?|race cars?|sports cars?|cars?|trucks?|vehicles?|suvs?|sedans?|bus|buses|automobiles?|buggy|jeeps?|vans?"),
    ("aircraft", r"airplanes?|aeroplanes?|aircraft|planes?|jet fighters?|airliners?|gliders?|biplanes?|drones?|uavs?|f-16|f-22|b-52|boeing|airbus"),
    ("aircraft", r"helicopters?|choppers?|rotorcraft"),
    ("rocket", r"rockets?|spacecraft|missiles?|saturn v|space shuttle|launch vehicle|boosters?"),
    ("building", r"skyscrapers?|buildings?|towers?|houses?|huts?|cabins?|sheds?|cathedrals?|church|pyramids?|temples?"),
    ("bottle", r"bottles?|flasks?|jugs?|vases?|cups?|mugs?|jars?|canteens?"),
    ("robot", r"robot arms?|robotic arms?|manipulators?|exoskeletons?|androids?|humanoids?|robots?"),
    ("bridge", r"bridges?|truss|trusses|suspension bridge|overpass"),
    ("turbine", r"wind turbines?|windmills?|wind mill|wind farm"),
    ("turbine", r"turbines?|fans?|propellers?|impellers?"),
    ("ship", r"ships?|boats?|submarines?|yachts?|sailboats?|frigates?|destroyers?"),
    ("unicycle", r"unicycles?"),
    ("bicycle", r"bikes?|bicycles?|motorcycles?|scooters?|motorbikes?"),
    ("engine", r"steam engines?|steam turbines?|boilers?|piston engines?|internal combustion|diesel engines?|gasoline engines?|stirling|watt engine"),
    ("engine", r"electric motors?|motors?|generators?|dynamos?|alternators?|transformers?|pumps?|compressors?|gearbox(?:es)?"),
    ("satellite", r"satellites?|space station|iss|hubble"),
    ("microscope", r"microscopes?|telescopes?|binoculars|periscopes?"),
    ("guitar", r"guitars?|violins?|cellos?|harps?"),
    ("racket", r"tennis rackets?|racquets?|rackets?|badminton|squash racket"),
]
_COMPILED = [(cat, re.compile(rf"\b(?:{pat})\b")) for cat, pat in _CATEGORIES]


def classify_topic(topic: str) -> str:
    t = (topic or "").lower()
    for cat, rx in _COMPILED:
        if rx.search(t):
            return cat
    return "generic"


def _c(text: str) -> str:
    return re.sub(r"[\x00-\x1f\x7f]+", " ", str(text))[:120]


def find_spec_mm(spec: SpecSheet | None, patterns: list[str], fallback_mm: float) -> float:
    if not spec or not spec.dimensions:
        return fallback_mm
    for pat in patterns:
        rx = re.compile(pat, re.I)
        for d in spec.dimensions:
            if (rx.search(d.key) or rx.search(d.label)) and d.unit_class == "length" and d.value > 0:
                return d.value
    return fallback_mm


def _f(x: float) -> str:
    return f"{x:.3f}"


def openscad_car_from_spec(topic: str, spec: SpecSheet | None = None) -> str:
    """Spec-grounded car: smooth hulls + cylinders. Overall length ~4.6 model units."""
    len_mm = find_spec_mm(spec, [r"overall.?length", r"total.?length", r"^length$"], 4515)
    wid_mm = find_spec_mm(spec, [r"overall.?width", r"max.?width", r"^width$"], 1852)
    hgt_mm = find_spec_mm(spec, [r"overall.?height", r"^height$"], 1310)
    wb_mm = find_spec_mm(spec, [r"wheelbase"], 2450)
    s = 4.6 / (len_mm / 1000)
    L, W, H, WB = (len_mm / 1000) * s, (wid_mm / 1000) * s, (hgt_mm / 1000) * s, (wb_mm / 1000) * s
    wr, ww = W * 0.19, W * 0.18
    ref = _c(spec.reference_product if spec and spec.reference_product else topic)
    return f"""
$fn = 48;
// {ref} — dimensions from verified spec (mm → scaled model units)
module wheel(r={_f(wr)}, w={_f(ww)}) {{
  rotate([90,0,0]) cylinder(h=w, r=r, center=true);
}}
// Low sporty chassis
hull() {{
  translate([0,0,{_f(H * 0.22)}]) cube([{_f(L)}, {_f(W)}, {_f(H * 0.38)}], center=true);
  translate([{_f(L * 0.05)},0,{_f(H * 0.48)}]) cube([{_f(L * 0.62)}, {_f(W * 0.82)}, {_f(H * 0.16)}], center=true);
}}
// Cabin bubble
translate([{_f(L * 0.08)},0,{_f(H * 0.62)}]) scale([1.0,0.58,0.42]) sphere(r={_f(H * 0.38)});
// Hood + rear deck taper
translate([{_f(L * 0.42)},0,{_f(H * 0.28)}]) scale([0.9,0.85,0.35]) sphere(r={_f(W * 0.42)});
translate([{_f(-L * 0.38)},0,{_f(H * 0.32)}]) scale([0.7,0.8,0.3]) sphere(r={_f(W * 0.38)});
// Wheels at spec wheelbase
translate([{_f(WB * 0.5)},  {_f(W * 0.46)}, 0]) wheel();
translate([{_f(WB * 0.5)},  {_f(-W * 0.46)}, 0]) wheel();
translate([{_f(-WB * 0.5)},  {_f(W * 0.48)}, 0]) wheel(r={_f(wr * 1.08)}, w={_f(ww * 1.1)});
translate([{_f(-WB * 0.5)},  {_f(-W * 0.48)}, 0]) wheel(r={_f(wr * 1.08)}, w={_f(ww * 1.1)});
// Headlights
translate([{_f(L * 0.47)}, {_f(W * 0.32)}, {_f(H * 0.22)}]) sphere(r={_f(W * 0.05)});
translate([{_f(L * 0.47)}, {_f(-W * 0.32)}, {_f(H * 0.22)}]) sphere(r={_f(W * 0.05)});
"""


def openscad_racket_from_spec(topic: str, spec: SpecSheet | None = None) -> str:
    """Spec-grounded tennis racket — oval head, string grid, handle + grip. Built along Z."""
    len_mm = find_spec_mm(spec, [r"overall.?length", r"total.?length", r"racket.?length", r"^length$"], 685)
    head_w_mm = find_spec_mm(spec, [r"head.?width", r"frame.?width", r"max.?width"], 290)
    head_l_mm = find_spec_mm(spec, [r"head.?length", r"head.?height"], 330)
    handle_mm = find_spec_mm(spec, [r"handle.?length", r"grip.?length"], 140)
    frame_mm = find_spec_mm(spec, [r"frame.?thickness", r"beam.?width"], 22)
    s = 4.0 / (len_mm / 1000)
    head_w = (head_w_mm / 1000) * s * 0.5
    head_h = (head_l_mm / 1000) * s * 0.5
    handle_l = (handle_mm / 1000) * s
    frame_t = max(0.025, (frame_mm / 1000) * s * 0.45)
    head_z = handle_l + head_h * 0.85
    ref = _c(spec.reference_product if spec and spec.reference_product else topic)
    return f"""
$fn = 64;
// {ref} — spec-grounded tennis racket
module oval_frame(rx, ry, t, z) {{
  difference() {{
    translate([0, 0, z]) scale([rx, ry, 1]) cylinder(h=t, r=1, center=true, $fn=96);
    translate([0, 0, z]) scale([rx - t * 2.2, ry - t * 2.2, 1]) cylinder(h=t + 0.02, r=1, center=true, $fn=96);
  }}
}}
module throat(z0, z1) {{
  hull() {{
    translate([0, 0, z0]) scale([1.1, 0.55, 1]) sphere(r={_f(head_w * 0.35)});
    translate([0, 0, z1]) scale([0.45, 0.35, 1]) sphere(r={_f(head_w * 0.22)});
  }}
}}
// Handle + grip
translate([0, 0, {_f(handle_l * 0.5)}]) cylinder(h={_f(handle_l)}, r={_f(head_w * 0.11)}, center=true, $fn=48);
for (i = [0:7])
  translate([0, 0, {_f(handle_l * 0.12)} + i * {_f(handle_l * 0.09)}])
    cylinder(h={_f(handle_l * 0.07)}, r={_f(head_w * 0.13)}, center=true, $fn=32);
translate([0, 0, {_f(handle_l * 0.04)}]) cylinder(h={_f(frame_t * 1.2)}, r={_f(head_w * 0.14)}, center=true, $fn=32);
// Throat + head frame
throat({_f(handle_l)}, {_f(handle_l + head_h * 0.25)});
oval_frame({_f(head_w)}, {_f(head_h)}, {_f(frame_t)}, {_f(head_z)});
// String bed
for (x = [{_f(-head_w * 0.78)} : {_f(head_w * 0.16)} : {_f(head_w * 0.78)}])
  translate([x, 0, {_f(head_z)}]) cube([{_f(frame_t * 0.22)}, {_f(head_h * 1.55)}, {_f(frame_t * 0.9)}], center=true);
for (y = [{_f(-head_h * 0.72)} : {_f(head_h * 0.14)} : {_f(head_h * 0.72)}])
  translate([0, y, {_f(head_z)}]) cube([{_f(head_w * 1.55)}, {_f(frame_t * 0.22)}, {_f(frame_t * 0.9)}], center=true);
"""


def openscad_aircraft_from_spec(topic: str, spec: SpecSheet | None = None) -> str:
    """Spec-grounded fixed-wing aircraft — fuselage, wings, tail, engines. Nose along +X."""
    len_mm = find_spec_mm(spec, [r"overall.?length", r"fuselage.?length", r"total.?length", r"^length$"], 70700)
    span_mm = find_spec_mm(spec, [r"wingspan", r"wing.?span", r"overall.?width"], 64040)
    hgt_mm = find_spec_mm(spec, [r"overall.?height", r"tail.?height", r"^height$"], 19240)
    s = 6.0 / (len_mm / 1000)
    L, W, H = (len_mm / 1000) * s, (span_mm / 1000) * s, (hgt_mm / 1000) * s
    fr = H * 0.12
    ref = _c(spec.reference_product if spec and spec.reference_product else topic)
    return f"""
$fn = 48;
// {ref} — spec-grounded airliner / fixed-wing aircraft
// Fuselage (nose +X)
hull() {{
  translate([{_f(L * 0.42)}, 0, {_f(H * 0.08)}]) scale([0.35, 0.42, 0.42]) sphere(r={_f(fr)});
  rotate([0, 90, 0]) translate([0, 0, {_f(H * 0.08)}]) cylinder(h={_f(L * 0.72)}, r={_f(fr)}, center=true);
  translate([{_f(-L * 0.38)}, 0, {_f(H * 0.1)}]) scale([0.55, 0.55, 0.55]) sphere(r={_f(fr)});
}}
// Main wings (mid fuselage)
translate([{_f(L * 0.02)}, 0, {_f(H * 0.06)}]) hull() {{
  cube([{_f(L * 0.22)}, {_f(W)}, {_f(H * 0.018)}], center=true);
  translate([{_f(L * 0.08)}, 0, 0]) cube([{_f(L * 0.08)}, {_f(W * 0.72)}, {_f(H * 0.012)}], center=true);
}}
// Horizontal stabilizer
translate([{_f(-L * 0.36)}, 0, {_f(H * 0.12)}])
  cube([{_f(L * 0.12)}, {_f(W * 0.38)}, {_f(H * 0.014)}], center=true);
// Vertical fin
translate([{_f(-L * 0.36)}, 0, {_f(H * 0.28)}])
  rotate([90, 0, 0]) cube([{_f(L * 0.11)}, {_f(H * 0.22)}, {_f(H * 0.012)}], center=true);
// Engine nacelles (under wings)
translate([0.000, {_f(W * 0.32)}, {_f(-H * 0.06)}])
  rotate([0, 90, 0]) cylinder(h={_f(L * 0.18)}, r={_f(fr * 0.55)}, center=true);
translate([0.000, {_f(-W * 0.32)}, {_f(-H * 0.06)}])
  rotate([0, 90, 0]) cylinder(h={_f(L * 0.18)}, r={_f(fr * 0.55)}, center=true);
// Cockpit glazing
translate([{_f(L * 0.32)}, 0, {_f(H * 0.14)}]) scale([0.55, 0.35, 0.28]) sphere(r={_f(fr * 0.85)});
"""


_SPEC_GROUNDED: dict[str, Callable[[str, SpecSheet | None], str]] = {
    "car": openscad_car_from_spec,
    "aircraft": openscad_aircraft_from_spec,
    "racket": openscad_racket_from_spec,
}


def template_for(topic: str, spec: SpecSheet | None = None, category: str | None = None) -> str | None:
    """OpenSCAD for a known category, or None for an object with no template."""
    cat = category or classify_topic(topic)
    if cat in _SPEC_GROUNDED:
        return _SPEC_GROUNDED[cat](topic, spec)
    static = _STATIC.get(cat)
    return static.replace("{TOPIC}", _c(topic)) if static else None


# Fixed templates, verbatim from Loomin's geometry-render route.
_STATIC = {
    "rocket": r"""$fn = 64;
// --- {TOPIC} ---
// Main body
cylinder(h=5.0, r=0.5, center=true);
// Nose cone
translate([0, 0, 2.8]) cylinder(h=1.5, r1=0.5, r2=0.02);
// 4 Fins
for(i=[0:3])
  rotate([0, 0, i*90])
    translate([0.5, 0, -1.8])
      rotate([0, 15, 0])
        cube([0.8, 0.06, 1.4], center=true);
// Engine nozzle
translate([0,0,-2.8]) cylinder(h=0.6, r1=0.3, r2=0.55);
// Booster ring
translate([0,0,-2.3]) difference() {
  cylinder(h=0.2, r=0.7, center=true);
  cylinder(h=0.25, r=0.5, center=true);
}
""",
    "building": r"""$fn = 32;
// --- {TOPIC} ---
// Main tower
cube([2.0, 2.0, 5.0], center=true);
// Entrance arch
translate([0, 1.05, -1.8])
  rotate([90,0,0])
    linear_extrude(0.2)
      polygon([[-.5,0],[.5,0],[.5,.7],[0,1.1],[-.5,.7]]);
// Windows (grid)
for(x=[-0.55,0,0.55], z=[-1.5,-0.5,0.5,1.5])
  translate([x, 1.05, z]) cube([0.3, 0.06, 0.4], center=true);
// Roof
translate([0,0,2.7]) cylinder(h=0.9, r1=1.5, r2=0.1);
""",
    "bottle": r"""$fn = 64;
// --- {TOPIC} ---
rotate_extrude()
  polygon([
    [0.00, 0.00],
    [0.55, 0.00],
    [0.55, 0.06],
    [0.62, 0.12],
    [0.62, 2.20],
    [0.50, 2.50],
    [0.25, 2.80],
    [0.25, 3.30],
    [0.32, 3.40],
    [0.32, 3.80],
    [0.25, 3.90],
    [0.00, 3.90]
  ]);
""",
    "robot": r"""$fn = 32;
// --- {TOPIC} ---
// Torso
cube([1.4, 0.9, 1.8], center=true);
// Head
translate([0, 0, 1.4]) sphere(r=0.55);
// Eyes
translate([ 0.22, 0.52, 1.52]) sphere(r=0.1);
translate([-0.22, 0.52, 1.52]) sphere(r=0.1);
// Arms
translate([ 0.95, 0, 0.2]) rotate([0,0,15]) cylinder(h=1.5, r=0.2, center=true);
translate([-0.95, 0, 0.2]) rotate([0,0,-15]) cylinder(h=1.5, r=0.2, center=true);
// Legs
translate([ 0.35, 0, -1.5]) cylinder(h=1.6, r=0.25, center=true);
translate([-0.35, 0, -1.5]) cylinder(h=1.6, r=0.25, center=true);
// Feet
translate([ 0.35, 0.2, -2.4]) cube([0.5, 0.8, 0.25], center=true);
translate([-0.35, 0.2, -2.4]) cube([0.5, 0.8, 0.25], center=true);
""",
    "bridge": r"""$fn = 16;
// --- {TOPIC} ---
// Deck
cube([8.0, 1.2, 0.2], center=true);
// Two towers
translate([ 2.5, 0, 1.2]) cube([0.3, 1.2, 2.4], center=true);
translate([-2.5, 0, 1.2]) cube([0.3, 1.2, 2.4], center=true);
// Suspension cables (simplified arcs)
for(y=[-0.5, 0.5]) {
  for(x=[-2.2,-1.5,-0.8,0,0.8,1.5,2.2])
    translate([x, y, 0.1])
      cylinder(h=2.4 - abs(x)*0.6, r=0.04);
}
// Side railings
translate([0,  0.65, 0.18]) cube([8.0, 0.06, 0.36], center=true);
translate([0, -0.65, 0.18]) cube([8.0, 0.06, 0.36], center=true);
// Supports / pylons
for(x=[-3.5,3.5])
  translate([x, 0, -0.5]) cylinder(h=1.0, r=0.25, center=true);
""",
    "turbine": r"""$fn = 64;
// --- {TOPIC} ---
// Tower
cylinder(h=4.5, r=0.18, center=true);
// Nacelle
translate([0,0,2.4]) rotate([0,90,0]) cylinder(h=1.0, r=0.28, center=true);
// Hub
translate([0.55,0,2.4]) sphere(r=0.22);
// 3 Blades
for(i=[0:2])
  rotate([i*120, 0, 0])
    translate([0.55, 0, 2.4])
      rotate([0, 8, 0])
        cube([0.12, 0.06, 2.8], center=true);
// Base foundation
translate([0,0,-2.4]) cylinder(h=0.4, r=0.7, center=true);
""",
    "ship": r"""$fn = 32;
// --- {TOPIC} ---
// Hull
hull() {
  translate([0,0,0]) cube([6.0, 2.2, 0.6], center=true);
  translate([3.2,0,0]) scale([0.4,0.6,1]) sphere(r=1.1);
  translate([-3.0,0,-0.15]) scale([0.3,0.7,0.5]) sphere(r=1.0);
}
// Superstructure
translate([-0.5, 0, 0.7]) cube([2.5, 1.6, 1.0], center=true);
translate([-0.3, 0, 1.3]) cube([1.5, 1.2, 0.5], center=true);
// Funnel/chimney
translate([-0.8, 0, 1.8]) cylinder(h=0.9, r=0.22, center=true);
// Mast
translate([2.0, 0, 0.8]) cylinder(h=2.2, r=0.06);
translate([-2.0, 0, 0.8]) cylinder(h=1.6, r=0.06);
// Deck details
for(x=[-2,-1,0,1,2])
  translate([x,0,0.34]) cylinder(h=0.15, r=0.1, center=true);
""",
    "bicycle": r"""$fn = 48;
// --- {TOPIC} ---
module wheel(r=0.55) {
  difference() {
    cylinder(h=0.08, r=r, center=true);
    cylinder(h=0.12, r=r-0.09, center=true);
  }
  for(i=[0:7]) rotate([0,0,i*45]) cube([r*1.88, 0.04, 0.08], center=true);
  cylinder(h=0.10, r=0.09, center=true);
}
translate([ 1.15, 0, 0]) rotate([90,0,0]) wheel();
translate([-1.15, 0, 0]) rotate([90,0,0]) wheel();
// Main frame (diamond)
hull() {
  translate([1.15,0,0]) sphere(r=0.07);
  translate([0,0,0.9]) sphere(r=0.06);
}
hull() {
  translate([-1.15,0,0]) sphere(r=0.07);
  translate([0,0,0.9]) sphere(r=0.06);
}
hull() {
  translate([-0.3,0,0.9]) sphere(r=0.05);
  translate([0.8,0,0.9]) sphere(r=0.05);
}
// Seat post + saddle
translate([-0.3,0,0.9]) cylinder(h=0.55, r=0.04, center=true);
translate([-0.3,0,1.2]) cube([0.42,0.18,0.06], center=true);
// Handlebars
translate([0.95,0,0.98]) rotate([0,90,0]) cylinder(h=0.55,r=0.04,center=true);
translate([0.95,0,0.98]) cube([0.06,0.62,0.06],center=true);
// Pedal cranks
translate([0,0,0]) rotate([90,0,0]) {
  cylinder(h=0.52,r=0.05,center=true);
  translate([0.22,0,0]) cylinder(h=0.06,r=0.07,center=true);
  translate([-0.22,0,0]) cylinder(h=0.06,r=0.07,center=true);
}
""",
    "unicycle": r"""$fn = 64;
// --- {TOPIC} ---
// Wheel with spokes and hub
difference() {
  cylinder(h=0.18, r=0.75, center=true);
  cylinder(h=0.22, r=0.62, center=true);
}
for(i=[0:7]) rotate([0,0,i*45]) cube([1.36, 0.05, 0.18], center=true);
cylinder(h=0.22, r=0.13, center=true);
// Tire tread (outer ring slightly wider)
difference() {
  cylinder(h=0.22, r=0.76, center=true);
  cylinder(h=0.25, r=0.72, center=true);
}
// Axle
rotate([90,0,0]) cylinder(h=0.5, r=0.04, center=true);
// Pedal cranks
rotate([90,0,0]) {
  translate([0.28,0,0]) { cylinder(h=0.3,r=0.04,center=true); translate([0.15,0,0]) cube([0.25,0.04,0.04],center=true); }
  translate([-0.28,0,0]) { cylinder(h=0.3,r=0.04,center=true); translate([-0.15,0,0]) cube([0.25,0.04,0.04],center=true); }
}
// Fork / bearing holder
translate([0.02,0,0.6]) rotate([2,0,0]) {
  translate([0, 0.12,0]) cylinder(h=1.1, r=0.04, center=true);
  translate([0,-0.12,0]) cylinder(h=1.1, r=0.04, center=true);
  translate([0,0,-0.55]) cylinder(h=0.06, r=0.18, center=true);
}
// Seat post
translate([0,0,1.25]) cylinder(h=1.0, r=0.04, center=true);
// Saddle
translate([0,0,1.78]) hull() {
  translate([-0.18,0,0]) sphere(r=0.08);
  translate([ 0.18,0,0]) sphere(r=0.07);
}
""",
    "engine": r"""$fn = 48;
// --- {TOPIC} (steam engine) ---
// Boiler — large horizontal cylinder (the heart of a steam engine)
rotate([0,90,0]) cylinder(h=3.8, r=0.82, center=true);
// Boiler end caps
translate([ 1.95,0,0]) sphere(r=0.82);
translate([-1.95,0,0]) sphere(r=0.82);
// Chimney stack
translate([1.5, 0, 1.05]) cylinder(h=2.0, r=0.22);
// Chimney cap/flare
translate([1.5, 0, 3.0]) cylinder(h=0.2, r1=0.22, r2=0.32);
// Firebox (rectangular box at rear of boiler)
translate([-1.6, 0, -0.5]) cube([1.2, 1.5, 0.9], center=true);
// Cylinder (working cylinder, horizontal)
translate([0.6, 0.9, 0.6]) rotate([0,90,0]) cylinder(h=1.2, r=0.28, center=true);
// Cylinder end cover
translate([1.22, 0.9, 0.6]) sphere(r=0.29);
translate([-0.02, 0.9, 0.6]) sphere(r=0.29);
// Piston rod (extends from cylinder)
translate([1.55, 0.9, 0.6]) rotate([0,90,0]) cylinder(h=0.7, r=0.07, center=true);
// Crosshead
translate([1.92, 0.9, 0.6]) cube([0.18, 0.35, 0.25], center=true);
// Connecting rod (to flywheel crank)
hull() {
  translate([1.92, 0.9, 0.6]) sphere(r=0.06);
  translate([-1.2, 0.9, 0.1]) sphere(r=0.06);
}
// Flywheel (large spoked wheel)
translate([-1.6, 1.1, 0.1]) rotate([90,0,0]) {
  difference() {
    cylinder(h=0.14, r=1.05, center=true);
    cylinder(h=0.18, r=0.85, center=true);
  }
  for(i=[0:5]) rotate([0,0,i*60]) cube([1.82, 0.09, 0.14], center=true);
  cylinder(h=0.18, r=0.14, center=true);
}
// Valve chest
translate([0.6, 0.55, 0.85]) cube([1.0, 0.4, 0.28], center=true);
// Steam pipe from boiler to valve
hull() {
  translate([0.7,0,0.82]) sphere(r=0.07);
  translate([0.7,0.55,0.82]) sphere(r=0.07);
}
// Base/bedplate
translate([0, 0, -1.0]) cube([5.0, 2.0, 0.22], center=true);
// Two upright frame columns
translate([ 0.6, 0, -0.55]) cube([0.14, 1.9, 1.0], center=true);
translate([-1.2, 0, -0.55]) cube([0.14, 1.9, 0.8], center=true);
// Pressure gauge on boiler top
translate([0.2, 0, 0.9]) cylinder(h=0.18, r=0.22, center=true);
translate([0.2, 0, 1.0]) sphere(r=0.19);
""",
    "satellite": r"""$fn = 32;
// --- {TOPIC} ---
// Main body (bus)
cube([1.4, 1.0, 1.8], center=true);
// Solar panels (2 large wings)
translate([ 1.7, 0, 0]) cube([2.4, 0.06, 1.2], center=true);
translate([-1.7, 0, 0]) cube([2.4, 0.06, 1.2], center=true);
// Panel frames
for(x=[-1.7,1.7], z=[-0.55,0,0.55])
  translate([x,0,z]) cube([2.4,0.09,0.05],center=true);
// Antenna dish
translate([0, 0, 1.3]) {
  cylinder(h=0.08, r1=0.7, r2=0.5);
  translate([0,0,0.3]) cylinder(h=0.5, r=0.04, center=true);
}
// Thruster nozzles
for(x=[-0.45,0.45], y=[-0.45,0.45])
  translate([x,y,-1.0]) cylinder(h=0.25, r1=0.05, r2=0.12);
// Payload (telescope or sensor box)
translate([0,0.6,0.5]) cube([0.4,0.3,0.5], center=true);
""",
    "microscope": r"""$fn = 32;
// --- {TOPIC} ---
// Base
cylinder(h=0.18, r=1.1, center=true);
// Arm / column
translate([0.2,0,0]) cylinder(h=3.8, r=0.18);
// Stage (specimen platform)
translate([0.2,0,1.4]) cylinder(h=0.12, r=0.7, center=true);
// Stage clips
translate([0.2, 0.55, 1.42]) cube([0.5,0.06,0.08],center=true);
translate([0.2,-0.55, 1.42]) cube([0.5,0.06,0.08],center=true);
// Objective turret
translate([0.2,0,2.1]) cylinder(h=0.3, r=0.42, center=true);
// Objectives (3 lenses)
for(i=[0:2]) rotate([0,0,i*120])
  translate([0.2+0.3,0,2.0]) cylinder(h=0.5, r=0.08);
// Eyepiece tube + eyepiece
translate([0.2,0,3.85]) cylinder(h=0.55, r=0.19, center=true);
translate([0.2,0,4.15]) cylinder(h=0.2, r1=0.22, r2=0.19, center=true);
// Coarse focus knob
translate([0.2+0.18,0,2.5]) rotate([90,0,0]) cylinder(h=0.08,r=0.28,center=true);
""",
    "guitar": r"""$fn = 48;
// --- {TOPIC} ---
// Body — figure-8 shape using two offset hulls
hull() {
  translate([0, 0.55, 0]) cylinder(h=0.34, r=0.85, center=true);
  translate([0,-0.55, 0]) cylinder(h=0.34, r=0.85, center=true);
}
// Waist pinch
difference() {
  cube([2,2,0.36],center=true);
  translate([ 0.72,0,0]) cylinder(h=0.4,r=0.45,center=true);
  translate([-0.72,0,0]) cylinder(h=0.4,r=0.45,center=true);
}
// Sound hole
cylinder(h=0.4, r=0.3, center=true);
// Neck
translate([0,1.52,0.04]) cube([0.28,1.8,0.22],center=true);
// Frets (6)
for(i=[0:5]) translate([0,0.72+i*0.18,0.14]) cube([0.28,0.03,0.04],center=true);
// Headstock
translate([0,2.5,0.06]) cube([0.38,0.45,0.24],center=true);
// Tuning pegs (6)
for(x=[-0.14,0.14], y=[2.36,2.5,2.64])
  translate([x,y,0.22]) cylinder(h=0.18,r=0.04,center=true);
// Bridge
translate([0,-0.8,0.18]) cube([0.7,0.1,0.1],center=true);
// Strings (6 thin cylinders)
for(x=[-0.09,-0.05,-0.01,0.01,0.05,0.09])
  translate([x,0.9,0.2]) cube([0.015,3.6,0.015],center=true);
""",
}
