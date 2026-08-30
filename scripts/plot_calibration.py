"""Render the calibration overlay (measured vs fitted model) as a deck-ready SVG.

    python -m scripts.plot_calibration                    # reads the saved fit
    python -m scripts.plot_calibration --which dht22      # a selftest variant

Reads evals/results_calibration.json (written by scripts.fit_rc) and writes
evals/calibration_overlay.svg — a static, self-contained SVG for the deck and
report. HONESTY IS IN THE ARTIFACT: a selftest fit renders with a loud
"SYNTHETIC SELF-TEST — NOT HARDWARE DATA" banner baked into the image, so the
chart cannot be pasted into a slide as something it is not. Only a
kind="measured" result renders clean, and the capability gate still decides
whether it enters the deck.

Design notes (kept consistent with the dashboard's chart language):
  measured  = solid blue line (#2a78d6), direct-labelled at the line end
  model fit = dashed near-black line (#0b0b0b) — hue + dash, never color alone
  heater-on = amber spans (#fab219 at low opacity), named in the legend line
  grid/axes recessive (#e1e0d9 / #52514e), white surface, no decoration
Stdlib only.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent.parent
IN_FILE = ROOT / "evals" / "results_calibration.json"
OUT_FILE = ROOT / "evals" / "calibration_overlay.svg"

INK, INK2, MUTED, GRID = "#0b0b0b", "#52514e", "#898781", "#e1e0d9"
BLUE, AMBER, CRIT, SURFACE = "#2a78d6", "#fab219", "#d03b3b", "#fcfcfb"


def render_svg(result: dict) -> str:
    """One calibration result dict (fit_rc.fit output) -> SVG text.

    INPUT: dict with kind, series{t_s, measured_c, simulated_c, heater},
      fit{R_K_per_W, C_J_per_K, tau_s, rmse_c}, power_w, ambient_c, n_samples.
    OUTPUT: complete <svg> document string.
    ERROR STATES: ValueError when the series is missing or too short.
    """
    s = result.get("series") or {}
    t = [float(x) / 60.0 for x in s.get("t_s") or []]          # minutes
    meas = [float(x) for x in s.get("measured_c") or []]
    simd = [float(x) for x in s.get("simulated_c") or []]
    heat = [int(x) for x in s.get("heater") or []]
    if len(t) < 3 or len(t) != len(meas) or len(t) != len(simd):
        raise ValueError("result carries no chartable series — re-run scripts.fit_rc")
    fit = result.get("fit") or {}
    selftest = result.get("kind") != "measured"

    W, H = 820, 440
    L, R, T0, B = 64, 118, selftest and 92 or 66, 54
    pw, ph = W - L - R, H - T0 - B
    lo = min(min(meas), min(simd)) - 0.4
    hi = max(max(meas), max(simd)) + 0.4
    x0, x1 = t[0], t[-1]
    X = lambda v: L + pw * (v - x0) / max(1e-9, x1 - x0)
    Y = lambda v: T0 + ph * (1 - (v - lo) / max(1e-9, hi - lo))

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
           f'font-family="system-ui, -apple-system, Segoe UI, sans-serif">',
           f'<rect width="{W}" height="{H}" fill="{SURFACE}"/>']

    # heater spans first, under everything
    span = None
    for i in range(len(t)):
        on = i < len(heat) and heat[i]
        if on and span is None:
            span = t[i]
        if (not on or i == len(t) - 1) and span is not None:
            end = t[i] if on else t[max(0, i - 1)]
            out.append(f'<rect x="{X(span):.1f}" y="{T0}" '
                       f'width="{max(1.0, X(end) - X(span)):.1f}" height="{ph}" '
                       f'fill="{AMBER}" fill-opacity="0.10"/>')
            span = None

    # grid + y labels
    for g in range(5):
        gy = T0 + ph * g / 4
        gv = hi - (hi - lo) * g / 4
        out.append(f'<line x1="{L}" y1="{gy:.1f}" x2="{L + pw}" y2="{gy:.1f}" '
                   f'stroke="{GRID}" stroke-width="1"/>')
        out.append(f'<text x="{L - 8}" y="{gy + 4:.1f}" text-anchor="end" '
                   f'font-size="12" fill="{MUTED}">{gv:.1f}</text>')
    # x labels: start / heater markers handled by spans / end
    for v, anchor in ((x0, "start"), (x1, "end")):
        out.append(f'<text x="{X(v):.1f}" y="{H - B + 20}" text-anchor="middle" '
                   f'font-size="12" fill="{MUTED}">{v:.0f} min</text>')
    out.append(f'<text x="{L + pw / 2}" y="{H - 14}" text-anchor="middle" '
               f'font-size="12" fill="{INK2}">minutes into the step-response run '
               f'(amber = heater commanded on)</text>')
    out.append(f'<text x="20" y="{T0 + ph / 2}" font-size="12" fill="{INK2}" '
               f'transform="rotate(-90 20 {T0 + ph / 2})" text-anchor="middle">'
               f'box air temperature (&#176;C)</text>')

    path = lambda ys: " ".join(f'{"M" if i == 0 else "L"}{X(t[i]):.1f} {Y(v):.1f}'
                               for i, v in enumerate(ys))
    out.append(f'<path d="{path(simd)}" fill="none" stroke="{INK}" '
               f'stroke-width="2" stroke-dasharray="6 5"/>')
    out.append(f'<path d="{path(meas)}" fill="none" stroke="{BLUE}" stroke-width="2.2"/>')
    out.append(f'<text x="{L + pw + 8}" y="{Y(meas[-1]) + 4:.1f}" font-size="13" '
               f'font-weight="600" fill="{BLUE}">measured</text>')
    dy = 18 if abs(Y(simd[-1]) - Y(meas[-1])) < 14 else 4
    out.append(f'<text x="{L + pw + 8}" y="{Y(simd[-1]) + dy:.1f}" font-size="13" '
               f'fill="{INK}">model fit</text>')

    # title + fit annotation
    out.append(f'<text x="{L}" y="30" font-size="18" font-weight="650" fill="{INK}">'
               f'Digital twin calibrated against hardware: step response</text>')
    ann = (f'fitted R = {fit.get("R_K_per_W", "?")} K/W · '
           f'C = {fit.get("C_J_per_K", "?")} J/K · '
           f'&#964; = {fit.get("tau_s", "?")} s · '
           f'RMSE = {fit.get("rmse_c", "?")} &#176;C · '
           f'{result.get("n_samples", "?")} samples · heater {result.get("power_w", "?")} W')
    out.append(f'<text x="{L}" y="50" font-size="12.5" fill="{INK2}">{ann}</text>')

    if selftest:
        out.append(f'<rect x="{L}" y="60" width="{pw + R - 10}" height="24" rx="6" '
                   f'fill="{CRIT}" fill-opacity="0.10" stroke="{CRIT}"/>')
        out.append(f'<text x="{L + 10}" y="77" font-size="13" font-weight="650" '
                   f'fill="{CRIT}">SYNTHETIC SELF-TEST &#8212; NOT HARDWARE DATA '
                   f'(source: {escape(str(result.get("source", "?")))})</text>')

    out.append("</svg>")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--infile", type=Path, default=IN_FILE)
    ap.add_argument("--out", type=Path, default=OUT_FILE)
    ap.add_argument("--which", default="sht31",
                    help="selftest variant to chart when the file holds a selftest bundle")
    args = ap.parse_args()

    data = json.loads(args.infile.read_text(encoding="utf-8"))
    if "results" in data:                     # a selftest bundle: pick one variant
        result = data["results"][args.which]
    else:
        result = data
    svg = render_svg(result)
    args.out.write_text(svg, encoding="utf-8")
    kind = result.get("kind")
    print(f"Saved -> {args.out}  (kind={kind}"
          + (", banner baked in: NOT hardware data" if kind != "measured" else "") + ")")


if __name__ == "__main__":
    main()
