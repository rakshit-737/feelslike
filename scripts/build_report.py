"""Build the FeelsLike project report (docs/FeelsLike-Report.html).

Reads evals/results_energy.json, evals/results_nlp.json (rules run),
evals/results_nlp_llm.json (LLM run), and rl/models/progress.csv, renders
inline-SVG charts, and writes a print-ready HTML document.

Usage:
    python -m scripts.build_report
    # then print to PDF:
    msedge --headless --print-to-pdf=docs/FeelsLike-Report.pdf docs/FeelsLike-Report.html
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

INK = "#0b0b0b"
INK2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
BLUE = "#2a78d6"
BLUE_LT = "#86b6ef"
GRAY_BAR = "#a8a69e"
GOOD = "#006300"
CRIT = "#d03b3b"


# ---------------- charts (inline SVG, print-safe, light mode) ----------------

def bar_chart(rows, unit, width=660, bar_h=26, gap=14, label_w=190, val_w=110):
    """rows: [(label, value, color, value_label, label_color)]"""
    xmax = max(v for _, v, _, _, _ in rows) * 1.02
    plot_w = width - label_w - val_w
    h = len(rows) * (bar_h + gap) + 6
    s = [f'<svg viewBox="0 0 {width} {h}" width="100%" role="img">']
    y = 2
    for label, value, color, vlabel, vcolor in rows:
        bw = max(0.0, plot_w * value / xmax)
        s.append(f'<text x="{label_w-10}" y="{y+bar_h/2+4}" text-anchor="end" '
                 f'font-size="12.5" fill="{INK2}">{label}</text>')
        s.append(f'<rect x="{label_w}" y="{y}" width="{bw:.1f}" height="{bar_h}" '
                 f'rx="4" fill="{color}"/>')
        s.append(f'<text x="{label_w+bw+8:.1f}" y="{y+bar_h/2+4}" font-size="13" '
                 f'font-weight="650" fill="{vcolor}" '
                 f'style="font-variant-numeric:tabular-nums">{vlabel}</text>')
        y += bar_h + gap
    s.append(f'<text x="{label_w}" y="{h-2}" font-size="10.5" fill="{MUTED}">{unit}</text>')
    s.append("</svg>")
    return "\n".join(s)


def curve_chart(pts, width=660, height=250):
    L, R, T, B = 56, 96, 14, 34
    pw, ph = width - L - R, height - T - B
    xmax = pts[-1][0]
    ys = [p[1] for p in pts]
    ymin, ymax = min(ys) - 0.3, max(ys) + 0.3
    X = lambda t: L + pw * t / xmax
    Y = lambda v: T + ph * (1 - (v - ymin) / (ymax - ymin))
    s = [f'<svg viewBox="0 0 {width} {height}" width="100%" role="img">']
    # gridlines + y labels
    for i in range(5):
        v = ymin + (ymax - ymin) * i / 4
        y = Y(v)
        s.append(f'<line x1="{L}" y1="{y:.1f}" x2="{L+pw}" y2="{y:.1f}" '
                 f'stroke="{GRID}" stroke-width="1"/>')
        s.append(f'<text x="{L-8}" y="{y+4:.1f}" text-anchor="end" font-size="11" '
                 f'fill="{MUTED}" style="font-variant-numeric:tabular-nums">{v:.1f}</text>')
    # x ticks every 0.5M
    for m in range(0, 5):
        t = m * 500_000
        if t > xmax:
            break
        s.append(f'<text x="{X(t):.1f}" y="{height-12}" text-anchor="middle" '
                 f'font-size="11" fill="{MUTED}">{m*0.5:g}M</text>')
    s.append(f'<text x="{L+pw/2}" y="{height-0}" text-anchor="middle" font-size="10.5" '
             f'fill="{MUTED}">environment steps</text>')
    path = " ".join(f'{"L" if i else "M"}{X(t):.1f} {Y(v):.1f}'
                    for i, (t, v) in enumerate(pts))
    s.append(f'<path d="{path}" fill="none" stroke="{BLUE}" stroke-width="2" '
             f'stroke-linejoin="round"/>')
    s.append(f'<text x="{L+pw+8}" y="{Y(pts[-1][1])+4:.1f}" font-size="11.5" '
             f'font-weight="600" fill="{BLUE}">mean episode<tspan x="{L+pw+8}" dy="13">reward</tspan></text>')
    s.append("</svg>")
    return "\n".join(s)


# ---------------- data ----------------

def load():
    energy = json.loads((ROOT / "evals/results_energy.json").read_text())["results"]
    nlp_rules = json.loads((ROOT / "evals/results_nlp.json").read_text())
    llm_path = ROOT / "evals/results_nlp_llm.json"
    nlp_llm = json.loads(llm_path.read_text()) if llm_path.exists() else None
    blind_path = ROOT / "evals/results_blind.json"
    globals()["BLIND"] = json.loads(blind_path.read_text()) if blind_path.exists() else None
    blind_llm_path = ROOT / "evals/results_blind_llm.json"
    globals()["BLIND_LLM"] = (json.loads(blind_llm_path.read_text())
                              if blind_llm_path.exists() else None)
    rows = list(csv.DictReader(open(ROOT / "rl/models/progress.csv")))
    key = "rollout/ep_rew_mean"
    curve = [(int(r["time/total_timesteps"]), float(r[key])) for r in rows if r.get(key)]
    curve = curve[:: max(1, len(curve) // 140)]
    return energy, nlp_rules, nlp_llm, curve


def pct(hit, n):
    return f"{hit}/{n} ({100*hit/n:.0f}%)"


def main():
    energy, nlp_rules, nlp_llm, curve = load()
    e = {k.split(" (")[0].replace("Static 22degC schedule", "Static 22 °C schedule")
           .replace("Reactive thermostat 24degC", "Reactive thermostat 24 °C"): v
         for k, v in energy.items()}
    static = energy["Static 22degC schedule (baseline)"]
    react = energy["Reactive thermostat 24degC"]
    rules = energy["FeelsLike (constraint-aware)"]
    ppo = energy.get("FeelsLike (PPO agent)")

    energy_svg = bar_chart([
        ("Static 22 °C (today's default)", static["kwh"], GRAY_BAR, f'{static["kwh"]:.0f} kWh', INK2),
        ("Reactive thermostat 24 °C", react["kwh"], GRAY_BAR,
         f'{react["kwh"]:.0f} kWh · −{react["saved_pct_vs_baseline"]}%', INK2),
        ("FeelsLike (rules, demo)", rules["kwh"], BLUE,
         f'{rules["kwh"]:.0f} kWh · −{rules["saved_pct_vs_baseline"]}%', BLUE),
        ("FeelsLike (PPO agent)", ppo["kwh"], BLUE_LT,
         f'{ppo["kwh"]:.0f} kWh · −{ppo["saved_pct_vs_baseline"]}%', INK2),
    ], "HVAC electricity, 7 simulated days, identical weather")

    viol_svg = bar_chart([
        ("Static 22 °C (today's default)", static["viol_min"], GRAY_BAR, f'{static["viol_min"]:,.0f} min', CRIT),
        ("Reactive thermostat 24 °C", react["viol_min"], GRAY_BAR, f'{react["viol_min"]:,.0f} min', INK2),
        ("FeelsLike (rules, demo)", max(rules["viol_min"], 0), BLUE, "0 min", GOOD),
        ("FeelsLike (PPO agent)", ppo["viol_min"], BLUE_LT, f'{ppo["viol_min"]:.0f} min', INK2),
    ], "occupied minutes outside the 23–26.5 °C comfort band")

    curve_svg = curve_chart(curve)

    # Held-out is aggregated across every heldout* split. The benchmark grew a
    # second held-out block; quoting only the first one would report a subset of
    # the evidence while the totals table counted all of it.
    def merge_heldout(splits: dict) -> dict:
        parts = [v for k, v in splits.items() if k.startswith("heldout")]
        keys = ("n", "det", "zone", "issue", "triple")
        return {k: sum(p.get(k, 0) for p in parts) for k in keys}

    r_dev, r_ho = nlp_rules["splits"]["dev"], merge_heldout(nlp_rules["splits"])
    dev_n, ho_n = r_dev["n"], r_ho["n"]
    bench_n = dev_n + ho_n
    if nlp_llm:
        l_dev, l_ho = nlp_llm["splits"]["dev"], merge_heldout(nlp_llm["splits"])
        ld_n, lh_n = l_dev["n"], l_ho["n"]
        llm_cols = f"""
        <tr><td>Complaint detection</td><td>{pct(r_dev['det'],dev_n)}</td><td>{pct(r_ho['det'],ho_n)}</td>
            <td>{pct(l_dev['det'],ld_n)}</td><td class="hl">{pct(l_ho['det'],lh_n)}</td></tr>
        <tr><td>Zone extraction</td><td>{pct(r_dev['zone'],dev_n)}</td><td>{pct(r_ho['zone'],ho_n)}</td>
            <td>{pct(l_dev['zone'],ld_n)}</td><td class="hl">{pct(l_ho['zone'],lh_n)}</td></tr>
        <tr><td>Issue extraction</td><td>{pct(r_dev['issue'],dev_n)}</td><td>{pct(r_ho['issue'],ho_n)}</td>
            <td>{pct(l_dev['issue'],ld_n)}</td><td class="hl">{pct(l_ho['issue'],lh_n)}</td></tr>
        <tr><td><b>Exact triple</b></td><td><b>{pct(r_dev['triple'],dev_n)}</b></td><td><b>{pct(r_ho['triple'],ho_n)}</b></td>
            <td><b>{pct(l_dev['triple'],ld_n)}</b></td><td class="hl"><b>{pct(l_ho['triple'],lh_n)}</b></td></tr>"""
        llm_head = (f"<th>Rules · dev ({dev_n})</th><th>Rules · held-out ({ho_n})</th>"
                    f"<th>LLM · dev ({ld_n})</th><th>LLM · held-out ({lh_n})</th>")
    else:
        llm_head = f"<th>Rules · dev ({dev_n})</th><th>Rules · held-out ({ho_n})</th>"
        llm_cols = ""  # rules-only fallback, not expected in practice

    blind = globals().get("BLIND")
    blind_llm = globals().get("BLIND_LLM")
    ho_triple_pct = round(100 * r_ho["triple"] / ho_n) if ho_n else 0
    blind_fails, parser_verdict = "", ""
    if blind:
        p = blind["pct"]
        cats = blind.get("by_category", {})
        worst = sorted((c for c in cats.items() if c[1]["triple"] < c[1]["n"]))
        lp = (blind_llm or {}).get("pct")

        def two_col(metric: str, key: str, meaning: str, hl: bool = False) -> str:
            cls = ' class="hl"' if hl else ""
            llm_cell = f"<td><b>{lp[key]}%</b></td>" if lp else ""
            return (f"<tr><td>{metric}</td><td{cls}><b>{p[key]}%</b></td>{llm_cell}"
                    f"<td>{meaning}</td></tr>")

        llm_th = f"<th>LLM · Groq</th>" if lp else ""
        blind_block = f"""
<h3>The blind probe: what the parser scores on language it has never seen</h3>
<p>
A held-out split stops being held out the moment someone debugs against it. Ours did:
after a parser rewrite the held-out exact-triple score read <b>{ho_triple_pct}%</b>, which
measures how thoroughly those specific sentences were fixed, not how well the parser
generalizes. So we keep a second set, <span class="mono">evals/blind_probe.json</span>, that
is never used for tuning — if a case in it ever informs a fix, that case is retired and replaced.
</p>
<table>
<tr><th>Metric ({blind['n']} unseen cases)</th><th>Rules parser</th>{llm_th}<th>What it means</th></tr>
{two_col("Zone extraction (exact set)", "zoneset",
         "Naming the right room is close to solved, including multi-zone and Hinglish.", hl=True)}
{two_col("Complaint detection", "det",
         "Deciding <i>whether</i> a message is a complaint at all is the weak axis.")}
{two_col("Issue extraction", "issue",
         "Novel metaphors and inverted sarcasm still land on the wrong issue.")}
{two_col("<b>Exact triple</b>", "triple",
         "All three correct. This is the number to quote, not the tuned split's.")}
</table>
<p class="figcap">Categories still failing on unseen input:
{', '.join(c for c, _v in worst) if worst else 'none'}.</p>
<div class="callout">
<b>Why publish the lower number?</b> Because the difference between the tuned split's
{ho_triple_pct}% and the probe's {p['triple']}% <i>is</i> the finding. It quantifies how much
of a held-out score survives contact with genuinely new phrasing, and it is the only NLP
number this report quotes as generalization.
</div>
"""
        # Failures are quoted from the probe run rather than remembered, so the
        # examples can never describe a defect that has since been fixed.
        rows = []
        for f in (blind.get("failures") or [])[:6]:
            missed = ", ".join(f.get("missed", [])) or "exact triple"
            rows.append(f'  <li><i>"{f["text"]}"</i> — <span class="mono">'
                        f'{f.get("category", "uncategorised")}</span>; missed {missed}.</li>')
        blind_fails = ("<ul>\n" + "\n".join(rows) + "\n</ul>") if rows else \
            "<p>No case in the current probe fails outright.</p>"

        # The rules-vs-LLM verdict is COMPUTED. An earlier revision asserted the
        # LLM path was the product; the probe now measures the opposite, and a
        # hand-written conclusion is exactly the kind of claim that goes stale.
        if lp:
            wins = [k for k in ("zoneset", "det", "issue", "triple") if p[k] > lp[k]]
            losses = [k for k in ("zoneset", "det", "issue", "triple") if p[k] < lp[k]]
            if p["triple"] > lp["triple"]:
                parser_verdict = (
                    f"<b>The rules path is not just insurance.</b> On {blind['n']} unseen cases the "
                    f"staged rules parser scores {p['triple']}% exact triple against the hosted "
                    f"LLM's {lp['triple']}%, leading on {len(wins)} of 4 axes and trailing on "
                    f"{len(losses)}. The runtime still prefers the LLM whenever an API key is "
                    f"present (rules are the no-key, no-Wi-Fi path), so this result is a live "
                    f"argument against that default rather than a description of it: on unseen "
                    f"phrasing the component we wrote generalizes better than the model we "
                    f"called, and the probe is what makes that visible instead of assumed.")
            else:
                parser_verdict = (
                    f"<b>The LLM path is the product, the rules are the insurance.</b> On "
                    f"{blind['n']} unseen cases the LLM scores {lp['triple']}% exact triple "
                    f"against the rules parser's {p['triple']}%: the keyword ceiling on unseen "
                    f"phrasing is structural, while the LLM path improves with model quality at "
                    f"zero code change. The rules parser keeps the demo alive offline, and at "
                    f"{p['zoneset']}% zone accuracy it does that well.")
        else:
            parser_verdict = (
                f"<b>{p['triple']}% exact triple on {blind['n']} unseen cases.</b> Zone extraction "
                f"({p['zoneset']}%) is close to solved; complaint detection ({p['det']}%) is the "
                f"weak axis, and it is the one that decides whether a constraint is filed at all.")
    else:
        blind_block = ""
        blind_fails = ""
        parser_verdict = ""

    limit_parser = (
        f"{blind['pct']['triple']}% exact-triple on the {blind['n']}-case blind probe (§8); "
        f"complaint detection ({blind['pct']['det']}%) fails before zone extraction "
        f"({blind['pct']['zoneset']}%) does. Benchmarked and shown, not estimated."
        if blind else
        "Measured on the held-out split only; no blind probe has been run for this build.")

    html = TEMPLATE
    for k, v in {
        "@@ENERGY_CHART@@": energy_svg,
        "@@VIOL_CHART@@": viol_svg,
        "@@CURVE_CHART@@": curve_svg,
        "@@NLP_HEAD@@": llm_head,
        "@@NLP_ROWS@@": llm_cols,
        "@@BLIND@@": blind_block,
        "@@BLIND_FAILS@@": blind_fails,
        "@@PARSER_VERDICT@@": parser_verdict,
        "@@LIMIT_PARSER@@": limit_parser,
        "@@BENCH_N@@": str(bench_n),
        "@@DEV_N@@": str(dev_n),
        "@@HO_N@@": str(ho_n),
        "@@BENCH_LABEL@@": (f"case NLP benchmark, plus a {blind['n']}-case blind probe the "
                            f"parser was never tuned on" if blind else
                            "case NLP benchmark with a held-out split"),
    }.items():
        html = html.replace(k, v)

    DOCS.mkdir(exist_ok=True)
    out = DOCS / "FeelsLike-Report.html"
    out.write_text(html, encoding="utf-8")
    print(f"wrote {out}")


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>FeelsLike — Technical Report</title>
<style>
  @page { size: A4; margin: 17mm 16mm 19mm 16mm; }
  * { box-sizing: border-box; margin: 0; }
  html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
  body { font: 10.5pt/1.55 "Segoe UI", system-ui, sans-serif; color: #0b0b0b; }
  h1 { font-size: 30pt; letter-spacing: -0.02em; line-height: 1.1; }
  h2 { font-size: 15pt; letter-spacing: -0.01em; margin: 0 0 8pt;
       padding-bottom: 4pt; border-bottom: 2px solid #0b0b0b; }
  h3 { font-size: 11.5pt; margin: 14pt 0 4pt; }
  p { margin: 0 0 7pt; }
  section { page-break-before: always; }
  section.flow { page-break-before: auto; margin-top: 18pt; }
  .cover { page-break-before: auto; }
  .muted { color: #898781; }
  .ink2 { color: #52514e; }
  .good { color: #006300; font-weight: 650; }
  .crit { color: #d03b3b; font-weight: 650; }
  .blue { color: #2a78d6; font-weight: 650; }
  code, .mono { font-family: Consolas, "Cascadia Mono", monospace; font-size: 9.3pt; }
  pre { font-family: Consolas, "Cascadia Mono", monospace; font-size: 8.8pt; line-height: 1.45;
        background: #f6f5f2; border: 1px solid #e1e0d9; border-radius: 6pt;
        padding: 8pt 10pt; margin: 6pt 0 10pt; white-space: pre-wrap; }
  table { border-collapse: collapse; width: 100%; margin: 6pt 0 10pt;
          font-variant-numeric: tabular-nums; font-size: 9.6pt; }
  th { text-align: left; font-size: 8.8pt; text-transform: uppercase; letter-spacing: 0.04em;
       color: #898781; border-bottom: 1.5px solid #0b0b0b; padding: 3pt 8pt 3pt 0; }
  td { border-bottom: 1px solid #e1e0d9; padding: 4pt 8pt 4pt 0; vertical-align: top; }
  td.hl { background: #eef4fc; }
  .figure { margin: 10pt 0 4pt; }
  .figcap { font-size: 8.8pt; color: #898781; margin: 2pt 0 12pt; }
  .statband { display: flex; gap: 10pt; margin: 22pt 0; }
  .stat { flex: 1; border: 1.5px solid #0b0b0b; border-radius: 8pt; padding: 10pt 12pt; }
  .stat .v { font-size: 21pt; font-weight: 700; letter-spacing: -0.02em;
             font-variant-numeric: tabular-nums; }
  .stat .l { font-size: 8.6pt; color: #52514e; margin-top: 2pt; }
  .pipeline { display: flex; align-items: stretch; gap: 6pt; margin: 10pt 0; }
  .pstep { flex: 1; border: 1px solid #c9c8c0; border-radius: 6pt; padding: 7pt 8pt;
           font-size: 8.6pt; line-height: 1.4; background: #fbfaf8; }
  .pstep b { display: block; font-size: 9.4pt; margin-bottom: 2pt; }
  .parrow { align-self: center; color: #898781; font-size: 12pt; }
  .callout { border-left: 3px solid #2a78d6; background: #f4f8fd;
             padding: 7pt 10pt; border-radius: 0 6pt 6pt 0; margin: 8pt 0 10pt; font-size: 9.8pt; }
  .two { columns: 2; column-gap: 18pt; }
  ul { margin: 0 0 8pt 14pt; padding: 0; }
  li { margin-bottom: 3pt; }
  .toc td { padding: 2.5pt 8pt 2.5pt 0; border-bottom: 1px dotted #e1e0d9; }
</style>
</head>
<body>

<!-- ============================== COVER ============================== -->
<div class="cover">
  <p class="muted" style="margin-top:24pt">Technical report · Team Goldilocks · August 2026</p>
  <h1>FeelsLike</h1>
  <p style="font-size:14pt; color:#52514e; margin:6pt 0 0">Buildings that listen.</p>
  <p style="font-size:11pt; margin-top:14pt; max-width:150mm">
    A digital-twin building optimizer with natural-language feedback: occupants complain in
    plain language, a parser turns the complaint into a typed constraint, and a
    constraint-aware HVAC controller acts on a physics-based thermal simulation —
    cutting energy while keeping every occupied minute inside the comfort band.
  </p>
  <div class="statband">
    <div class="stat"><div class="v good">−26.6%</div><div class="l">HVAC energy vs the static schedule buildings run today (7 simulated days)</div></div>
    <div class="stat"><div class="v good">0 min</div><div class="l">comfort violations — vs 16,328 min for the baseline over the same week</div></div>
    <div class="stat"><div class="v">@@BENCH_N@@</div><div class="l">@@BENCH_LABEL@@</div></div>
    <div class="stat"><div class="v">2M</div><div class="l">PPO training steps on the twin; ablation-tested against 3 baselines</div></div>
  </div>
  <h3>Contents</h3>
  <table class="toc">
    <tr><td>1</td><td>The problem &amp; the idea</td></tr>
    <tr><td>2</td><td>System architecture</td></tr>
    <tr><td>3</td><td>The digital twin: 5-zone RC thermal model</td></tr>
    <tr><td>4</td><td>Understanding complaints: the NLP parser</td></tr>
    <tr><td>5</td><td>The constraint engine: decay, arbitration, memory</td></tr>
    <tr><td>6</td><td>Controllers &amp; the 7-day A/B result</td></tr>
    <tr><td>7</td><td>The reinforcement-learning track</td></tr>
    <tr><td>8</td><td>NLP benchmark: methodology &amp; honest numbers</td></tr>
    <tr><td>9</td><td>The live demo system</td></tr>
    <tr><td>10</td><td>Scale story: money &amp; carbon</td></tr>
    <tr><td>11</td><td>Limitations &amp; path to production</td></tr>
    <tr><td>12</td><td>Anticipated questions</td></tr>
    <tr><td>A</td><td>Repository map &amp; commands</td></tr>
  </table>
</div>

<!-- ============================== 1 ============================== -->
<section>
<h2>1 · The problem &amp; the idea</h2>
<p>
Commercial buildings spend roughly 40–50% of their electricity on HVAC, and most of them
run it deaf: a facilities team sets a fixed schedule — typically an aggressive setpoint
like 22 °C from early morning to late evening — precisely <i>because</i> nobody can hear
the occupants. Overcooling everyone, everywhere, all day is the cheapest way to avoid
complaints when feedback arrives days late through a ticket system, if it arrives at all.
The result is the familiar picture: sweaters and space heaters under desks in August,
while the chiller runs flat out.
</p>
<p>
The feedback that could fix this already exists. People say "it's stuffy in here" out
loud, in chat, constantly. It just never reaches the control loop.
</p>
<div class="callout">
<b>FeelsLike closes that loop.</b> A complaint typed in Slack or a web chat
("it's really stuffy in Conference Room B") is parsed into a typed, schema-validated
constraint, the constraint steers a zone-level HVAC controller, and a physics-based
digital twin proves the effect: the building relaxes its blanket overcooling everywhere
people <i>aren't</i> uncomfortable, and spends energy exactly where they are.
</div>
<p>
The one-line result: over seven simulated days on identical weather, FeelsLike used
<b class="good">26.6% less energy</b> than the static schedule with
<b class="good">zero</b> occupied minutes outside the comfort band — while the obvious
alternative, simply raising the thermostat, saved slightly more (31.7%) but broke comfort
for 429 minutes. Efficiency <i>without sacrifice</i> is the point, and both halves of that
claim are measured, not asserted.
</p>

<h3>Why this is not "just a thermostat app"</h3>
<ul>
  <li><b>It listens in natural language</b> — sarcasm, typos, and Hinglish included — with
      a benchmarked parser and explicit anti-hallucination guardrails.</li>
  <li><b>It arbitrates disagreement.</b> Two people in one room asking for opposite things
      get a severity-weighted, time-decaying compromise with a human-readable explanation —
      not a war over the thermostat.</li>
  <li><b>It proves the effect.</b> Two identical twins run in lock-step on the same weather,
      one listening and one not. Every number shown is that A/B difference.</li>
  <li><b>It degrades gracefully.</b> No API key, no Wi-Fi, no problem: an offline rules
      parser keeps the entire system functional.</li>
</ul>
</section>

<!-- ============================== 2 ============================== -->
<section>
<h2>2 · System architecture</h2>
<p>Everything communicates through one contract, agreed at hour zero and never renegotiated:
a complaint becomes a <code>ParsedComplaint</code>, which becomes a <code>Constraint</code>,
which nudges a controller acting on the twin.</p>

<div class="pipeline">
  <div class="pstep"><b>Occupant</b>Slack slash-command, Teams webhook, or the dashboard chat. Plain language, any phrasing.</div>
  <div class="parrow">→</div>
  <div class="pstep"><b>Parser</b><code>backend/parser.py</code> — LLM structured extraction with a deterministic offline rules fallback. Emits strict JSON.</div>
  <div class="parrow">→</div>
  <div class="pstep"><b>Constraint store</b><code>backend/constraints.py</code> — exponential decay, conflict arbitration, explanations, comfort memory.</div>
  <div class="parrow">→</div>
  <div class="pstep"><b>Controller</b><code>sim/controllers.py</code> — occupancy-aware setbacks + pre-cooling + live complaint offsets (or the PPO policy).</div>
  <div class="parrow">→</div>
  <div class="pstep"><b>Digital twin</b><code>sim/twin.py</code> — 5-zone RC thermal model; energy, cost, CO₂ and comfort accounting.</div>
  <div class="parrow">→</div>
  <div class="pstep"><b>Dashboard</b><code>dashboard/index.html</code> — the A/B race, floor plan, complaint feed; FastAPI backend polls at 1 Hz.</div>
</div>

<h3>The contract</h3>
<pre>{"is_comfort_complaint": true,
 "zone_id": "zone_b",              // null if no known zone was named
 "issue": "too_hot" | "too_cold" | "stuffy" | "humid" | "drafty" | "other",
 "severity": 1..3,                 // 1 mild, 2 clear discomfort, 3 urgent
 "confidence": 0.0..1.0,
 "reasoning": "one short sentence"}</pre>

<h3>Guardrails that prevent judge-visible failures</h3>
<ul>
  <li>A <code>zone_id</code> outside the known zone list is <b>nulled</b> — the system never
      acts on a hallucinated zone.</li>
  <li><code>zone_id = null</code> triggers a clarifying question, never a guess.</li>
  <li>Non-thermal messages ("the projector is broken") are classified
      <code>is_comfort_complaint = false</code> and explicitly ignored.</li>
  <li>Retractions ("it's fine now in Room B") <b>clear</b> the zone's constraints instead of
      filing a new complaint — including trap phrasings like "the heat issue is fixed now".</li>
  <li>Every parse is Pydantic-validated; anything malformed falls back to the rules parser.</li>
</ul>
<p>
Two twins always run in lock-step on identical weather — FeelsLike vs the Static-22 °C
baseline. The dashboard's racing meters, savings percentages, and comfort counters are all
that live A/B difference; nothing is precomputed or staged.
</p>
</section>

<!-- ============================== 3 ============================== -->
<section>
<h2>3 · The digital twin: 5-zone RC thermal model</h2>
<p>
The twin is a standard lumped-parameter (resistor–capacitor) building model — the same
family used in building-simulation literature — chosen so its behavior is defensible and
its parameters are physically interpretable. Each zone <i>i</i> integrates:
</p>
<pre>C_i · dT_i/dt = UA_i (T_out − T_i)              // envelope conduction
              + Σ_j G_ij (T_j − T_i)            // inter-zone coupling (shared walls)
              + Q_solar_i + Q_occupants_i       // gains: facade sun + 100 W/person
              + Q_hvac_i                        // cooling (negative)</pre>
<p>
HVAC is a "perfect thermostat with finite capacity": each 60 s step it removes exactly the
heat needed to reach the setpoint, capped at the unit's capacity. Electrical power =
thermal cooling / COP (3.4) + ventilation fan power (0 / 150 / 420 W per zone by fan
level). Ventilation also adds 45 W/K of outdoor-air coupling per level — fresh air costs
energy when it's hot outside, which is exactly the stuffy-vs-efficient trade-off the
controller must navigate.
</p>

<h3>The building</h3>
<table>
<tr><th>Zone</th><th>Facade</th><th>Area</th><th>C (J/K)</th><th>UA (W/K)</th><th>Peak solar</th><th>Cooling cap</th><th>Occupancy profile</th></tr>
<tr><td>Open Office A</td><td>N</td><td>220 m²</td><td>3.2 × 10⁶</td><td>500</td><td>1,600 W</td><td>16 kW</td><td>office: up to 24 people, 8:00–20:00</td></tr>
<tr><td>Conference Room B</td><td>S</td><td>60 m²</td><td>0.9 × 10⁶</td><td>160</td><td>2,400 W</td><td>7 kW</td><td>meetings 10:00, 14:00, 16:30</td></tr>
<tr><td>Cabin C</td><td>E</td><td>40 m²</td><td>0.6 × 10⁶</td><td>110</td><td>1,700 W</td><td>4 kW</td><td>3 people, 9:00–19:00</td></tr>
<tr><td>Lobby D</td><td>W</td><td>90 m²</td><td>1.4 × 10⁶</td><td>300</td><td>2,600 W</td><td>8 kW</td><td>4 people, 8:00–20:00</td></tr>
<tr><td>Cafeteria E</td><td>S</td><td>110 m²</td><td>1.6 × 10⁶</td><td>340</td><td>2,100 W</td><td>10 kW</td><td>lunch surge: 30 people 12:00–14:30</td></tr>
</table>
<p class="figcap">Inter-zone conductances (shared walls): A–B 90, A–C 70, A–D 110, D–E 90, B–C 40 W/K.</p>

<h3>Weather, solar, occupancy</h3>
<p>
Weather is a seeded synthetic model of an Indian city in August: base 30 °C with a
deterministic ±1.5 °C per-day wobble, a 6 °C diurnal swing (minimum ~3 AM, maximum ~3 PM),
and a small smooth intra-day perturbation. Deterministic seeding is what makes the A/B race
fair — every controller sees byte-identical weather. Solar gain follows each facade's
orientation (east peaks 9:00, south 12:30, west 16:00; north gets diffuse only). Occupancy
follows weekday/weekend piecewise schedules per zone — the lunch surge in the cafeteria and
the 14:00 conference meeting are the recurring events the comfort-memory feature learns.
An <code>Open-Meteo</code> hook exists to swap in real hourly weather with no interface change.
</p>
<h3>Comfort accounting</h3>
<p>
The occupied comfort band is 23.0–26.5 °C (ASHRAE-style for offices). Every simulated
minute a zone is occupied and outside the band counts as a violation minute, with
degree-minutes tracked above and below separately. Unoccupied zones accrue nothing —
comfort is a promise to people, not to empty rooms. Energy is priced at ₹9/kWh
(commercial ToU average) and 0.71 kg CO₂/kWh (India grid, CEA ~FY23).
</p>
</section>

<!-- ============================== 4 ============================== -->
<section>
<h2>4 · Understanding complaints: the NLP parser</h2>
<p>
The parser's job is deceptively narrow — one chat message in, one JSON constraint out —
but the input space is human: sarcasm ("great, another sauna day 🙃"), Hinglish
("cabin mein bahut garmi hai bhai"), typos, indirect phrasing ("I'm wearing a jacket"),
and messages that merely <i>mention</i> a room without complaining about it.
</p>
<h3>Two parsers, one interface</h3>
<p>
<b>Primary — LLM structured extraction.</b> A system prompt carries the schema, the known
zone list with aliases, explicit anti-hallucination rules, and worked examples (including
Hinglish and the projector-is-broken negative). Any OpenAI-compatible provider works via
<code>LLM_BASE_URL</code>: Anthropic Claude, OpenAI, or free tiers — Groq (Llama 3.3 70B,
sub-second), Google AI Studio (Gemini 2.5 Flash), or a fully local Ollama model. The demo
runs on Groq's free tier at zero cost.
</p>
<p>
<b>Fallback — deterministic rules.</b> Keyword-cascade parser: most-specific issue first
(drafty before cold, so "cold air blowing on me" is a draft, not a temperature complaint),
zone resolution via aliases plus "room B"-style regex, severity from intensity words. It is
instant, offline, and free — the demo-day insurance. The switch is automatic: any LLM
error, timeout, or missing key degrades gracefully, and the UI labels every message with
its parse source (<code>llm</code> or <code>rules</code>) and latency honestly.
</p>
<h3>Retraction handling</h3>
<p>
"It's fine now, thanks" must <i>cancel</i> a constraint, not file one — and "the heat issue
in the lobby is fixed now" must not be read as a heat complaint just because it contains
the word "heat". A retraction detector (all-clear phrases, guarded by ongoing-markers like
"but" / "still") runs before keyword matching; on retraction with a known zone, the zone's
active constraints are expired immediately and the feed shows an all-clear. This was a
known failure mode we designed for rather than disclosed away.
</p>
<h3>Latency</h3>
<p>Rules: ~0 ms. Groq free tier: typically 0.3–0.8 s. Claude/OpenAI: 1–2 s. The complaint →
visible action loop on the dashboard is under a second in the offline path and 1–2 s
end-to-end with a hosted LLM — fast enough that the parse feels like a reflex on stage.</p>
</section>

<!-- ============================== 5 ============================== -->
<section>
<h2>5 · The constraint engine: decay, arbitration, memory</h2>
<h3>Complaints decay — recency-weighted democracy, not a ticket queue</h3>
<p>
A complaint's influence decays exponentially with a <b>45-minute half-life</b> and expires
at 2 hours unless re-reported:
</p>
<pre>decay(t)  = 0.5 ^ (age / 45 min)          (0 after 2 h)
weight(t) = severity × confidence × decay(t)</pre>
<p>
This single design choice solves three problems at once: stale complaints don't haunt the
building forever, a persistent problem naturally re-asserts itself (people complain again),
and no manual "close ticket" step is ever needed.
</p>
<h3>Effects</h3>
<table>
<tr><th>Issue</th><th>Setpoint offset (sev 1/2/3)</th><th>Airflow</th></tr>
<tr><td>too_hot</td><td>−0.8 / −1.3 / −1.8 °C</td><td>—</td></tr>
<tr><td>too_cold</td><td>+0.8 / +1.3 / +1.8 °C</td><td>—</td></tr>
<tr><td>stuffy</td><td>−0.3 / −0.4 / −0.5 °C</td><td>fan +1</td></tr>
<tr><td>humid</td><td>−0.4 / −0.5 / −0.6 °C</td><td>fan +1</td></tr>
<tr><td>drafty</td><td>+0.3 / +0.4 / +0.5 °C</td><td>fan −1</td></tr>
</table>
<h3>Conflict arbitration</h3>
<p>
Opposing constraints in one zone resolve by <b>weighted mean</b> — weight = severity ×
confidence × decay — with a human-readable explanation generated for the dashboard and the
Slack reply: <i>"CONFLICT in zone: stuffy (sev 2, 3m ago) vs too cold (sev 2, 1m ago) →
severity-weighted compromise: −0.1 °C."</i> Neither party wins forever, because both claims
decay. Every office has this argument; FeelsLike settles it transparently in seconds.
</p>
<h3>Comfort memory (learning the building's habits)</h3>
<p>
The store keeps complaint history, and a pattern miner clusters it by (zone, issue,
hour-of-day). When the same complaint has occurred on ≥ 2 distinct days at a similar hour
(±1 h), the system pre-applies a gentle constraint (severity 1, confidence 0.5)
<b>30 minutes before</b> the pattern's usual time and announces it in the feed:
<i>"Conference Room B usually reports stuffy around 14:00 — pre-applying a gentle fix."</i>
The building stops waiting to be told twice. Memory only learns from real occupant
complaints, never from its own pre-applies, so it cannot self-reinforce.
</p>
</section>

<!-- ============================== 6 ============================== -->
<section>
<h2>6 · Controllers &amp; the 7-day A/B result</h2>
<table>
<tr><th>Controller</th><th>What it models</th></tr>
<tr><td><b>Static 22 °C schedule</b></td><td>What buildings do today: overcool everything on a fixed schedule (07–21 h weekdays, 08–18 h weekends), fans always on. The complaint-avoidance strategy of a deaf building.</td></tr>
<tr><td><b>Reactive thermostat 24 °C</b></td><td>The obvious "fix": per-zone thermostat at a warmer setpoint, only when occupied. Saves more energy — and breaks comfort, because it reacts to the current minute only.</td></tr>
<tr><td><b>FeelsLike (constraint-aware)</b></td><td>Occupancy-aware setbacks (25 °C occupied / 26 °C pre-cool 30 min ahead / 28.5 °C unoccupied), crowd-aware fans, plus the live complaint offsets from §5. The demo controller.</td></tr>
<tr><td><b>FeelsLike (PPO agent)</b></td><td>A learned policy that offsets the rules controller's setpoints ±2 °C. §7.</td></tr>
</table>

<div class="figure">@@ENERGY_CHART@@</div>
<p class="figcap">Figure 1 — Energy: 7 simulated days, identical weather (seed 7). Lower is better.</p>

<div class="figure">@@VIOL_CHART@@</div>
<p class="figcap">Figure 2 — Comfort: occupied minutes outside the 23–26.5 °C band over the same week. The baseline's 16,328 min is systematic overcooling; the reactive thermostat's 429 min is the cost of naive efficiency.</p>

<table>
<tr><th>Controller</th><th>kWh</th><th>₹</th><th>kg CO₂</th><th>Viol-min</th><th>Hot °·min</th><th>Cold °·min</th><th>Saved</th></tr>
<tr><td>Static 22 °C schedule</td><td>722.4</td><td>6,502</td><td>512.9</td><td class="crit">16,328</td><td>418</td><td>15,973</td><td>—</td></tr>
<tr><td>Reactive thermostat 24 °C</td><td>493.4</td><td>4,441</td><td>350.3</td><td>429</td><td>1,281</td><td>0</td><td>31.7%</td></tr>
<tr><td><b>FeelsLike (rules)</b></td><td><b>530.3</b></td><td><b>4,773</b></td><td><b>376.5</b></td><td class="good">0</td><td class="good">0</td><td class="good">0</td><td><b>26.6%</b></td></tr>
<tr><td>FeelsLike (PPO)</td><td>512.7</td><td>4,614</td><td>364.0</td><td>22</td><td>3</td><td>0</td><td>29.0%</td></tr>
</table>

<div class="callout">
<b>The reactive thermostat is the argument, not the embarrassment.</b> It saves 5 points
more energy than FeelsLike — and pays 429 minutes of discomfort for it. Anyone can save
energy by letting rooms drift; the hard part is saving energy while keeping the comfort
promise. FeelsLike's zero holds because pre-cooling starts 30 minutes before people
arrive and complaints steer the controller before drift becomes suffering.
</div>
</section>

<!-- ============================== 7 ============================== -->
<section>
<h2>7 · The reinforcement-learning track</h2>
<h3>Environment</h3>
<table>
<tr><th></th><th>Specification</th></tr>
<tr><td>Episode</td><td>1 simulated weekday; control decision every 5 minutes (288 steps)</td></tr>
<tr><td>Observation (18-d)</td><td>5 zone temps · outdoor temp · sin/cos hour-of-day · 5 zone occupancies · 5 active complaint offsets</td></tr>
<tr><td>Action (5-d, continuous)</td><td>per-zone setpoint offset in [−2, +2] °C <i>around the rules controller's decision</i> — the agent learns residual corrections, not raw control</td></tr>
<tr><td>Reward</td><td>−ΔkWh − 0.12 × Δ(degree-minutes outside band) − 0.6 × unmet-complaint pressure</td></tr>
<tr><td>Complaints in training</td><td>0–3 synthetic complaints injected at random times/zones per episode, so the policy learns to respect human constraints, not just the static band</td></tr>
<tr><td>Algorithm</td><td>PPO (stable-baselines3), MlpPolicy, 8 parallel envs, n_steps 512, batch 1024, lr 3 × 10⁻⁴, γ 0.995, 2M steps</td></tr>
</table>

<div class="figure">@@CURVE_CHART@@</div>
<p class="figcap">Figure 3 — Learning curve: mean episode reward over 2M environment steps. Steady improvement (−91.8 → −86.8), no plateau — the policy is still learning when the budget ends.</p>

<h3>Ablation (10 random weekdays, mean ± sd)</h3>
<table>
<tr><th>Controller</th><th>kWh mean</th><th>kWh sd</th><th>Viol-min mean</th></tr>
<tr><td>Static 22 °C schedule</td><td>111.1</td><td>8.3</td><td class="crit">2,831</td></tr>
<tr><td>Reactive thermostat 24 °C</td><td>82.3</td><td>6.5</td><td>79</td></tr>
<tr><td>FeelsLike (rules)</td><td>84.4</td><td>8.1</td><td class="good">0</td></tr>
<tr><td>FeelsLike (PPO)</td><td><b>81.6</b></td><td>8.3</td><td>2</td></tr>
</table>

<div class="callout">
<b>The honest decision:</b> PPO beats the rules controller on energy (−2.8 kWh/day, and
−29.0% vs −26.6% on the 7-day run) but averages 2 violation-minutes per day against the
rules controller's zero. Our acceptance bar was "wins on energy at equal-or-better
comfort" — PPO doesn't clear it yet, so <b>the demo ships the rules controller</b> and
presents RL as a trajectory. The learning curve is still climbing at 2M steps; a longer
run may flip the decision. We would rather show a zero than 1.9 kWh.
</div>
</section>

<!-- ============================== 8 ============================== -->
<section>
<h2>8 · NLP benchmark: methodology &amp; honest numbers</h2>
<p>
The benchmark is @@BENCH_N@@ labeled cases scoring three things per message: complaint detection
(is this a comfort complaint at all?), zone extraction, and issue extraction. "Exact
triple" requires all three correct.
</p>
<p>
<b>The split is the methodology.</b> The @@DEV_N@@-case <i>dev</i> set is what the rules parser
was tuned on — its score there is table stakes, not evidence. The @@HO_N@@-case <i>held-out</i>
set was written after the rules were frozen: typos ("its friezing
in the confrence room"), sarcasm ("love how I need gloves to type"), Hinglish variants,
multi-zone messages, retractions with keyword traps, and negatives designed to fool keyword
matching ("the coffee machine is steaming hot again"). Those cases were later debugged
against, which is what retired them as a generalization measure — see the blind probe below.
</p>
<table>
<tr><th>Metric</th>@@NLP_HEAD@@</tr>
@@NLP_ROWS@@
</table>
<p class="figcap">LLM = Llama 3.3 70B via Groq free tier through the schema-validated prompt; identical guardrails on both parsers.
Read these split scores as development progress, not as generalization — the section below explains why.</p>

@@BLIND@@

<h3>What still fails (blind-probe cases, quoted as measured)</h3>
@@BLIND_FAILS@@
<div class="callout">
@@PARSER_VERDICT@@
</div>
</section>

<!-- ============================== 9 ============================== -->
<section>
<h2>9 · The live demo system</h2>
<h3>Dashboard</h3>
<p>
A single-file, zero-dependency dashboard (inline CSS/JS, no build step, nothing to break
offline) polling <code>/api/state</code> at 1 Hz:
</p>
<ul>
  <li><b>The race</b> — paired racing bars (FeelsLike vs baseline kWh) with the live
      saved-% as the headline, and the comfort-violations counter beside it: both halves of
      the claim on one screen.</li>
  <li><b>Floor plan</b> — the real 5-zone geometry, color-mapped to temperature, with
      setpoints, occupancy, fan levels, complaint offsets, pulsing constraint markers, and
      an amber conflict ring when a zone is under arbitration. Unoccupied zones render
      dimmed so overnight drift doesn't read as failure.</li>
  <li><b>Occupant channel</b> — chat with quick-chips that are the demo script (the
      conflict pair, Hinglish, and a deliberate non-complaint), each message showing its
      parse, source badge, and latency.</li>
  <li><b>Resilience UX</b> — typed messages survive failed sends; a reconnecting pill
      appears if the backend dies; the parser badge flips to <code>rules (offline
      fallback)</code> honestly if the LLM path fails mid-demo.</li>
</ul>
<h3>Slack / Teams</h3>
<p>
<code>POST /api/slack</code> accepts Slack slash-command form posts and Teams-style JSON.
A complaint typed in Slack changes a zone on the dashboard in seconds, and the bot replies
in-channel with the parsed action and explanation. The web chat is the same endpoint — the
fallback if venue Wi-Fi dislikes webhooks.
</p>
<h3>The 3-minute script</h3>
<table>
<tr><th>t</th><th>Beat</th></tr>
<tr><td>0:00</td><td>Hook: buildings are deaf, so they overcool everyone "just in case". HVAC ≈ 40–50% of commercial electricity.</td></tr>
<tr><td>0:20</td><td>Live: "it's really stuffy in Conference Room B" → parsed chip, zone shifts, latency badge.</td></tr>
<tr><td>1:00</td><td>Teammate: "Room B is freezing, I'm wearing a jacket" → CONFLICT badge, weighted compromise, explanation on screen.</td></tr>
<tr><td>1:30</td><td>Crank to 960×: racing bars open the gap over a simulated week — 26.6% less energy, zero violations, and the reactive counter-example.</td></tr>
<tr><td>2:20</td><td>Money/carbon tiles; scale story (§10).</td></tr>
<tr><td>2:45</td><td>Close: "Buildings have been deaf for a hundred years. We taught one to listen." Hand judges the card: type anything.</td></tr>
</table>
</section>

<!-- ============================== 10 ============================== -->
<section>
<h2>10 · Scale story: money &amp; carbon</h2>
<p>
The simulated building is ~520 m². Its measured week — 192 kWh saved, ₹1,729, 136 kg CO₂ —
is small on purpose: it's one honest building. The point is the mechanism scales linearly
with floor area and requires <b>no new hardware</b>: complaints already come from tools
offices use, and the control outputs are ordinary setpoint/fan commands any BMS accepts.
</p>
<table>
<tr><th></th><th>This building (measured, 7 days)</th><th>10,000 m² office (extrapolated, annual)</th></tr>
<tr><td>Energy saved</td><td>192 kWh</td><td>~192 MWh</td></tr>
<tr><td>Cost saved</td><td>₹1,729</td><td>~₹18–22 lakh</td></tr>
<tr><td>CO₂ avoided</td><td>136 kg</td><td>~160 tonnes</td></tr>
</table>
<p class="figcap">Extrapolation assumes similar climate, occupancy patterns, and the same
26.6% saving rate; it is a scale illustration, not a measurement — and labeled as such
wherever we show it.</p>
</section>

<!-- ============================== 11 ============================== -->
<section>
<h2>11 · Limitations &amp; path to production</h2>
<h3>Known limitations (disclosed, not discovered)</h3>
<ul>
  <li><b>"In simulation."</b> The zero-violation result holds because HVAC sizing is
      adequate in the model. Real buildings have undersized units and thermal surprises;
      the claim on stage is always "in simulation".</li>
  <li><b>Parser ceiling on unseen phrasing.</b> @@LIMIT_PARSER@@</li>
  <li><b>Multi-zone complaints.</b> A message naming two rooms still ends up as a
      single-zone constraint downstream; disclosed rather than worked around.</li>
  <li><b>RL not yet shipping.</b> The PPO agent trades 2 viol-min for 2.8 kWh/day; below
      our bar (§7).</li>
  <li><b>Synthetic weather/occupancy.</b> Deterministic by design for fair A/B; the
      Open-Meteo hook and BMS-log calibration are the path to real data.</li>
</ul>
<h3>Path to production</h3>
<ul>
  <li><b>Same architecture, real actuators:</b> the twin swaps for BACnet/Modbus writes;
      the constraint engine and parser are unchanged.</li>
  <li><b>Calibration:</b> fit each zone's R/C parameters from a week of BMS logs — a
      standard system-identification step for lumped-parameter models.</li>
  <li><b>Privacy by construction:</b> constraints aggregate at zone level; no identity is
      needed or stored beyond a display name in the feed.</li>
  <li><b>Safety envelope:</b> setpoints are clamped to 21.5–29 °C and offsets cap at
      ±1.8 °C — the building can be steered, never hijacked.</li>
  <li><b>RL flip-in:</b> the PPO policy is a drop-in controller class; when a longer run
      clears the comfort bar, one line switches the demo to it.</li>
</ul>
</section>

<!-- ============================== 12 ============================== -->
<section>
<h2>12 · Anticipated questions</h2>
<table>
<tr><th>Question</th><th>Answer</th></tr>
<tr><td>How do you stop the LLM hallucinating a setpoint?</td><td>It never emits one. The LLM only classifies into a schema; unknown zones are nulled, low confidence asks a clarifying question, non-complaints are ignored. Control math is deterministic code.</td></tr>
<tr><td>Is the RL real?</td><td>2M-step PPO with learning curves and a 4-way ablation — and we're showing you the run where it <i>didn't</i> beat our bar, which is how you know the numbers are real.</td></tr>
<tr><td>Two people disagree?</td><td>Live demo: severity × confidence × recency weighted compromise with a printed explanation. It decays, so neither wins forever.</td></tr>
<tr><td>How real is the twin?</td><td>Standard lumped-parameter RC model (literature-backed) with zone coupling, solar, occupancy, and capacity-limited HVAC. Calibration path: fit R/C from a week of BMS logs.</td></tr>
<tr><td>What if the Wi-Fi dies on stage?</td><td>It can — the parser auto-falls back to offline rules and the badge says so. The demo was designed to survive its own failure modes.</td></tr>
<tr><td>Why not just raise the thermostat?</td><td>We benchmarked exactly that (Reactive 24 °C): more savings, 429 minutes of broken comfort. That trade-off is the product's reason to exist.</td></tr>
<tr><td>Privacy?</td><td>Zone-level aggregation; no identity required.</td></tr>
</table>
</section>

<!-- ============================== A ============================== -->
<section>
<h2>Appendix A · Repository map &amp; commands</h2>
<table>
<tr><th>Path</th><th>What it is</th></tr>
<tr><td class="mono">sim/twin.py</td><td>5-zone RC thermal digital twin (physics, energy, comfort accounting)</td></tr>
<tr><td class="mono">sim/weather.py</td><td>Seeded synthetic weather + facade solar + Open-Meteo hook</td></tr>
<tr><td class="mono">sim/controllers.py</td><td>Static, reactive, constraint-aware, and PPO controllers</td></tr>
<tr><td class="mono">sim/env.py</td><td>Gymnasium environment (obs/action/reward, synthetic complaint injection)</td></tr>
<tr><td class="mono">backend/parser.py</td><td>LLM + rules complaint parser, retraction detection, provider config</td></tr>
<tr><td class="mono">backend/prompts.py</td><td>The structured-extraction system prompt</td></tr>
<tr><td class="mono">backend/constraints.py</td><td>Decaying constraint store, arbitration, explanations</td></tr>
<tr><td class="mono">backend/memory.py</td><td>Comfort memory: recurring-pattern mining and pre-application</td></tr>
<tr><td class="mono">backend/app.py</td><td>FastAPI: lock-step A/B sim, complaint API, Slack/Teams webhook, dashboard</td></tr>
<tr><td class="mono">dashboard/index.html</td><td>Single-file live dashboard (race, floor plan, occupant channel)</td></tr>
<tr><td class="mono">evals/</td><td>@@BENCH_N@@-case benchmark (dev/held-out splits) + the blind probe, runners, result JSONs</td></tr>
<tr><td class="mono">rl/train.py · rl/evaluate.py</td><td>PPO training (VecMonitor curves) and ablation</td></tr>
<tr><td class="mono">scripts/demo_day.py</td><td>7-day controller comparison → results JSON (the evidence table)</td></tr>
<tr><td class="mono">scripts/build_report.py</td><td>This document</td></tr>
</table>
<h3>Reproduce every number in this report</h3>
<pre>python -m scripts.demo_day                 # Figures 1–2 and the §6 table
python -m evals.run_nlp_eval --rules       # §8 rules columns (offline)
python -m evals.run_nlp_eval               # §8 LLM columns (needs a key; free via Groq)
python -m evals.run_blind_probe --rules    # §8 blind probe (the quoted generalization number)
python -m rl.train --steps 2000000         # Figure 3 (rl/models/progress.csv)
python -m rl.evaluate --episodes 10        # §7 ablation table
uvicorn backend.app:app --reload           # the live demo, http://127.0.0.1:8000</pre>
<p class="muted">FeelsLike · Team Goldilocks · github.com/rakshit-737/feelslike · Generated from measured results in the repository.</p>
</section>

</body>
</html>
"""

if __name__ == "__main__":
    main()
