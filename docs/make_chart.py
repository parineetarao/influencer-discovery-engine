"""Builds the README charts from sample_output.csv.

Optional helper (needs matplotlib: pip install matplotlib). Usage:
    python docs/make_chart.py sample_output.csv docs
Creates results_chart.png, score_breakdown.png, quota_bar.png and report_preview.png
in the output folder.
"""
import csv
import math
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch

src = sys.argv[1] if len(sys.argv) > 1 else "sample_output.csv"
outdir = sys.argv[2] if len(sys.argv) > 2 else "docs"

INK, MUTED, GRID = "#1f2937", "#6b7280", "#f0f1f3"
ACTIVE, INACTIVE = "#0f9d8a", "#c9ccd1"


def clamp(v):
    return max(0.0, min(1.0, v))


def load_rows():
    rows = []
    with open(src, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            try:
                rows.append({
                    "name": r["channel_name"], "score": int(r["potential_score"]),
                    "activity": r["activity"], "subs": int(r["subscribers"]),
                    "avg_views": float(r["avg_views_per_video"]),
                    "days": float(r["days_since_last_upload"]),
                    "gap": float(r["avg_days_between_uploads"]),
                })
            except (ValueError, KeyError):
                continue  # skip error rows / N/A values
    rows.sort(key=lambda x: x["score"])
    return rows


def style_axes(ax):
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color("#e5e7eb")
    ax.tick_params(axis="y", labelsize=11, colors=INK, length=0)
    ax.tick_params(axis="x", colors=MUTED)
    ax.xaxis.grid(True, color=GRID)
    ax.set_axisbelow(True)


def results_chart(rows):
    fig, ax = plt.subplots(figsize=(10, 0.55 * len(rows) + 2.2), dpi=200)
    bars = ax.barh([r["name"] for r in rows], [r["score"] for r in rows],
                   color=[ACTIVE if r["activity"] == "Active" else INACTIVE for r in rows],
                   height=0.62)
    for bar, r in zip(bars, rows):
        ax.text(r["score"] + 1.2, bar.get_y() + bar.get_height() / 2, str(r["score"]),
                va="center", fontsize=11, fontweight="bold", color=INK)
    ax.set_xlim(0, 112)
    ax.set_xlabel("Potential score (0-100)", color=MUTED, fontsize=10)
    style_axes(ax)
    fig.suptitle("Who is worth a call today?", x=0.02, y=0.98, ha="left",
                 fontsize=16, fontweight="bold", color=INK)
    ax.set_title("Potential score of %d creators found by the tool" % len(rows),
                 loc="left", fontsize=11, color=MUTED, pad=12)
    ax.legend(handles=[Patch(color=ACTIVE, label="Active (posted in the last 30 days)"),
                       Patch(color=INACTIVE, label="Inactive (score is halved)")],
              loc="lower right", frameon=False, fontsize=10)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    fig.savefig(os.path.join(outdir, "results_chart.png"), facecolor="white")


def score_breakdown(rows):
    parts = [("Engagement", "#0f766e"), ("Recency", "#14b8a6"),
             ("Reach", "#99e6da"), ("Consistency", "#f59e0b")]
    fig, ax = plt.subplots(figsize=(10, 0.55 * len(rows) + 2.4), dpi=200)
    names = [r["name"] for r in rows]
    left = [0.0] * len(rows)
    for idx, (label, color) in enumerate(parts):
        vals = []
        for r in rows:
            comp = [
                40 * clamp((r["avg_views"] / float(r["subs"])) / 0.25),
                25 * clamp((60 - r["days"]) / 53.0),
                20 * clamp((math.log10(r["subs"]) - 3) / 3.0),
                15 * clamp((30 - r["gap"]) / 27.0),
            ][idx]
            vals.append(comp * (0.5 if r["activity"] == "Inactive" else 1.0))
        ax.barh(names, vals, left=left, color=color, height=0.62, label=label)
        left = [l + v for l, v in zip(left, vals)]
    for y, r in enumerate(rows):
        ax.text(left[y] + 1.2, y, str(r["score"]), va="center", fontsize=11,
                fontweight="bold", color=INK)
    ax.set_xlim(0, 112)
    ax.set_xlabel("Points (inactive creators are halved)", color=MUTED, fontsize=10)
    style_axes(ax)
    fig.suptitle("What drives each score", x=0.02, y=0.98, ha="left",
                 fontsize=16, fontweight="bold", color=INK)
    ax.set_title("Four signals add up to 100 points", loc="left", fontsize=11,
                 color=MUTED, pad=12)
    ax.legend(loc="lower right", frameon=False, fontsize=10, ncol=2)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    fig.savefig(os.path.join(outdir, "score_breakdown.png"), facecolor="white")


def quota_bar(used=111, total=10000):
    fig, ax = plt.subplots(figsize=(10, 1.9), dpi=200)
    ax.barh([0], [total], color="#eef0f2", height=0.5)
    ax.barh([0], [used], color=ACTIVE, height=0.5)
    ax.set_xlim(0, total)
    ax.set_ylim(-0.6, 0.9)
    ax.axis("off")
    ax.text(0, 0.62, "A 10-creator audit uses about %.0f%% of the free daily quota" % (100.0 * used / total),
            fontsize=14, fontweight="bold", color=INK, va="bottom")
    ax.text(used + 120, 0, "%d units (1 search + 1 stats call + 10 upload lookups)" % used,
            fontsize=10, color=INK, va="center")
    ax.text(total, -0.45, "10,000 free units per day", fontsize=9, color=MUTED,
            ha="right", va="center")
    fig.savefig(os.path.join(outdir, "quota_bar.png"), facecolor="white",
                bbox_inches="tight", pad_inches=0.2)


def report_preview(limit=12):
    """A crisp, README-friendly picture of the report (instead of a wide spreadsheet screenshot)."""
    def num(value):
        try:
            return "{:,}".format(int(value))
        except (ValueError, TypeError):
            return str(value)

    with open(src, newline="", encoding="utf-8-sig") as f:
        data = list(csv.DictReader(f))[:limit]

    row_h = 0.42
    fig, ax = plt.subplots(figsize=(10, row_h * (len(data) + 1) + 1.0), dpi=200)
    ax.set_xlim(0, 1)
    ax.set_ylim(-(len(data) + 1) * row_h, 0.9)
    ax.axis("off")

    ax.text(0.0, 0.55, "sample_output.csv (first %d rows)" % len(data), fontsize=13,
            fontweight="bold", color=INK, va="center")

    # (header, key, x, alignment)
    cols = [("Creator", "channel_name", 0.01, "left"),
            ("Subscribers", "subscribers", 0.44, "right"),
            ("Total views", "total_views", 0.58, "right"),
            ("Last upload", "last_upload_date", 0.65, "left"),
            ("Activity", "activity", 0.79, "left"),
            ("Score", "potential_score", 0.985, "right")]

    ax.add_patch(plt.Rectangle((0, -row_h * 0.5), 1, row_h, color="#eef0f2", lw=0))
    for header, _, x, ha in cols:
        ax.text(x, 0, header, fontsize=10, fontweight="bold", color=MUTED, ha=ha, va="center")

    for i, r in enumerate(data):
        y = -(i + 1) * row_h
        if i % 2 == 1:
            ax.add_patch(plt.Rectangle((0, y - row_h * 0.5), 1, row_h, color="#f8fafc", lw=0))
        for header, key, x, ha in cols:
            value = r.get(key, "")
            color, weight = INK, "normal"
            if key in ("subscribers", "total_views"):
                value = num(value)
            elif key == "activity":
                color = ACTIVE if value == "Active" else MUTED
                weight = "bold"
            elif key == "potential_score":
                weight = "bold"
            ax.text(x, y, value, fontsize=10, color=color, fontweight=weight, ha=ha, va="center")
    fig.savefig(os.path.join(outdir, "report_preview.png"), facecolor="white",
                bbox_inches="tight", pad_inches=0.2)


rows = load_rows()
os.makedirs(outdir, exist_ok=True)
results_chart(rows)
score_breakdown(rows)
quota_bar()
report_preview()
print("saved charts to", outdir)