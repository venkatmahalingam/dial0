"""Draws docs/architecture.png, the Dial 0 architecture diagram used in the README.

Run: python3 docs/architecture.py   (needs matplotlib; uses the Poppins font if installed)"""
import math, os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Circle

# Poppins if it's installed (the look used in the slides), otherwise the default sans-serif font.
for d in (os.getenv("POPPINS_DIR", ""), "/usr/share/fonts/truetype/google-fonts/", os.path.expanduser("~/.fonts/")):
    if d and os.path.exists(os.path.join(d, "Poppins-Regular.ttf")):
        for f in ("Poppins-Regular.ttf", "Poppins-Medium.ttf", "Poppins-Bold.ttf", "Poppins-Italic.ttf"):
            if os.path.exists(os.path.join(d, f)):
                font_manager.fontManager.addfont(os.path.join(d, f))
        plt.rcParams["font.family"] = "Poppins"
        break

W, H = 100, 56.25
fig = plt.figure(figsize=(12, 6.75), dpi=160)
ax = fig.add_axes([0, 0, 1, 1]); ax.set_xlim(0, W); ax.set_ylim(-3.6, H - 3.6); ax.axis("off")
BG = "#F6F8FB"; fig.patch.set_facecolor(BG)
INK, SOFT, NAVY = "#1A2233", "#5F6B7D", "#0E2240"
PAL = {"blue": ("#E3EEFF", "#2F6FDB"), "amber": ("#FFF0D6", "#D98A00"), "green": ("#E1F6EA", "#23964F"),
       "rose": ("#FFE6E6", "#D04848"), "violet": ("#EEE8FF", "#6E4FD8"), "teal": ("#DDF4F2", "#1E8C84"),
       "white": ("#FFFFFF", "#C9D2DE"), "slate": ("#EDF1F6", "#41536B"), "card": ("#FFFFFF", "#D5DCE6")}


def box(x, y, w, h, color, r=1.2, lw=1.4, dash=False, shadow=True, z=2):
    fc, ec = PAL[color]
    if shadow:
        ax.add_patch(FancyBboxPatch((x + 0.25, y - 0.3), w, h, boxstyle=f"round,pad=0,rounding_size={r}",
                                    fc="#0E2240", ec="none", alpha=0.07, zorder=z - 0.5))
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle=f"round,pad=0,rounding_size={r}", fc=fc, ec=ec, lw=lw,
                                ls=(0, (4, 3)) if dash else "-", zorder=z))


def text(x, y, s, size=9, weight="regular", color=INK, ha="center", va="center", style="normal", z=10, **kw):
    ax.text(x, y, s, fontsize=size, fontweight=weight, color=color, ha=ha, va=va, style=style, zorder=z,
            linespacing=1.4, **kw)


def arrow(p1, p2, color="#41536B", rad=0.0, lw=1.5, label="", lpos=None, both=False, ls="-", z=9):
    ax.add_patch(FancyArrowPatch(p1, p2, arrowstyle="<|-|>" if both else "-|>", mutation_scale=11, lw=lw, color=color,
                                 connectionstyle=f"arc3,rad={rad}", shrinkA=1.5, shrinkB=1.5, zorder=z, ls=ls))
    if label:
        lx, ly = lpos or ((p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2)
        text(lx, ly, label, size=7.6, color=SOFT, style="italic", z=11, bbox=dict(fc="#FFFFFF", ec="none", pad=0.4))


def header(x, y, w, h, color, title, sub=None, tsize=9.6):
    box(x, y, w, h, color)
    if sub is None:
        text(x + w / 2, y + h / 2, title, size=tsize, weight="bold")


# ------------------------------------------------------------------ SONiC platform (outer)
box(17, 2.6, 81.4, 45.6, "slate", r=2.0, lw=2.2, shadow=False, z=1)
text(19, 46.6, "SONiC platform", size=13, weight="bold", ha="left", color=NAVY)
text(96.6, 46.6, "the switch", size=8.6, ha="right", color=SOFT, style="italic")

# ------------------------------------------------------------------ Dial 0 container
box(18.8, 4.4, 55.6, 40.4, "white", r=1.6, lw=1.6, shadow=True, z=2)
text(20.8, 43.2, "Dial 0 container", size=11, weight="bold", ha="left", color=NAVY)
text(72.6, 43.2, "agent harness", size=8.4, ha="right", color=SOFT, style="italic")

# local SLM
box(35.8, 35.6, 22.6, 6.0, "amber", z=3)
text(47.1, 39.6, "Local SLM", size=10, weight="bold")
text(47.1, 37.2, "Qwen3.5-4B  ·  llama.cpp  ·  CPU only", size=7.8)

# agent loop
cx, cy, R = 47.1, 24.6, 7.4
ax.add_patch(Circle((cx, cy), R, fc="#F2F6FF", ec="#2F6FDB", lw=1.3, zorder=3))
text(cx, cy + 0.9, "Agent", size=10, weight="bold", color="#2F6FDB")
text(cx, cy - 1.2, "loop", size=10, weight="bold", color="#2F6FDB")
steps = [("plan", 90, "amber"), ("check", 18, "green"), ("act", -54, "rose"), ("observe", -126, "teal"), ("fix", 162, "violet")]
pos = []
for name, ang, col in steps:
    x, y = cx + R * math.cos(math.radians(ang)), cy + R * math.sin(math.radians(ang))
    pos.append((x, y))
angs = [a for _, a, _ in steps]
for i in range(len(angs)):  # an arrowhead on the ring midway between each pair of steps (clockwise)
    a1, a2 = angs[i], angs[(i + 1) % len(angs)]
    if a2 > a1:
        a2 -= 360
    m = (a1 + a2) / 2
    p1 = (cx + R * math.cos(math.radians(m + 9)), cy + R * math.sin(math.radians(m + 9)))
    p2 = (cx + R * math.cos(math.radians(m - 9)), cy + R * math.sin(math.radians(m - 9)))
    ax.add_patch(FancyArrowPatch(p1, p2, arrowstyle="-|>", mutation_scale=13, lw=1.6, color="#2F6FDB",
                                 connectionstyle="arc3,rad=-0.08", shrinkA=0, shrinkB=0, zorder=7))
for (name, ang, col), (x, y) in zip(steps, pos):
    w = 1.0 * len(name) + 2.6
    box(x - w / 2, y - 1.3, w, 2.6, col, r=1.1, lw=1.3, shadow=False, z=6)
    text(x, y, name, size=8.4, weight="medium", z=12)

# memory + tools
box(20.6, 15.6, 14.8, 17.6, "blue", z=3)
text(28.0, 30.9, "Working context", size=9.4, weight="bold")
text(28.0, 29.3, "memory", size=9.4, weight="bold")
text(28.0, 22.6, "conversation context\nfollow-up requests\nwhat ran & what failed\nlearned plans\nworkflow results", size=7.7)
box(58.8, 15.6, 14.8, 17.6, "teal", z=3)
text(66.2, 30.9, "Tools", size=9.4, weight="bold")
text(66.2, 22.9, "SONiC CLI runner\ncommand reference\nlog search (grep)\nhealth & security checks\nCVE matcher", size=7.7)

# guardrails + workflows
box(20.6, 9.6, 53.0, 5.0, "green", z=3)
text(22.4, 12.1, "Guardrails", size=9.4, weight="bold", ha="left")
text(49.6, 12.1, "commands only from your reference  ·  checked against the switch's CLI\n"
     "values must come from you  ·  config-only changes  ·  y/N before every change", size=7.4)
box(20.6, 5.4, 53.0, 3.4, "violet", z=3)
text(22.4, 7.1, "Workflows", size=9.4, weight="bold", ha="left")
text(50.6, 7.1, "health · hardware · counters · security audit · CVE scan  —  scheduled, results kept", size=7.4)

# ------------------------------------------------------------------ SONiC stack
box(76.2, 4.4, 20.6, 40.4, "white", r=1.6, lw=1.6, z=2)
text(86.5, 43.2, "SONiC stack", size=11, weight="bold", color=NAVY)
layers = [("config / show CLI", "the operator's interface", "blue"),
          ("Redis databases", "CONFIG · APPL · STATE", "amber"),
          ("Containers", "swss · syncd · bgp · lldp · teamd", "teal"),
          ("SAI", "switch abstraction interface", "violet"),
          ("ASIC  ·  ports  ·  optics", "fans · SSD · power", "rose")]
for i, (t, sub, col) in enumerate(layers):
    y = 34.6 - i * 7.3
    box(77.8, y, 17.4, 6.0, col, r=1.0, lw=1.3, shadow=False, z=3)
    text(86.5, y + 3.75, t, size=8.6, weight="bold")
    text(86.5, y + 1.75, sub, size=7.0, color=SOFT)

# ------------------------------------------------------------------ outside: operator, at-a-glance, CVE feeds
box(1.6, 30.2, 13.6, 13.4, "card", z=2)
text(8.4, 41.1, "Operator", size=11, weight="bold")
text(8.4, 35.6, "asks in plain English\nor types SONiC\ncommands\n\napproves every change", size=7.6)

box(1.6, 4.4, 13.6, 8.4, "card", dash=True, shadow=False, z=2)
text(8.4, 10.4, "Public CVE feeds", size=9, weight="bold")
text(8.4, 7.2, "Debian Security Tracker\nCISA Known Exploited", size=7.4)

# ------------------------------------------------------------------ flows
arrow((15.2, 39.0), (43.6, 32.2), rad=-0.12, both=True, label="ask  ·  y/N", lpos=(24.0, 38.0))
arrow((47.1, 35.6), (47.1, 33.3), color="#D98A00", both=True)
text(52.6, 34.45, "plan · fix · insights", size=7.4, color=SOFT, style="italic", z=11)
arrow((35.4, 24.6), (39.6, 24.6), color="#2F6FDB", both=True)
arrow((54.6, 24.6), (58.8, 24.6), color="#2F6FDB", both=True)
arrow((73.6, 28.5), (77.8, 37.0), rad=0.12, label="runs", lpos=(75.2, 33.6))
arrow((77.8, 23.0), (73.6, 21.0), rad=0.12, label="reads", lpos=(75.6, 20.1))
arrow((15.2, 8.6), (20.6, 7.4), color="#8A97A8", ls="--")

text(98.4, 1.2, "everything runs on the switch  ·  nothing leaves the box", size=7.8, ha="right", color=SOFT, style="italic")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "architecture.png")
fig.savefig(OUT, dpi=160, facecolor=BG)
print("wrote", OUT)
