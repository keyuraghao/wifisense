"""A small design system for the dashboards.

Matplotlib defaults are built for print figures in papers: white ground, heavy
axes, a full box around every plot. On a screen, in a dark room, watching a live
signal, that is the wrong set of choices - the frame competes with the data and
the white ground is fatiguing over a long session.

So: dark ground, no box, one hairline grid, and colour reserved for meaning.
Every accent here maps to a state (idle, active, warning, alert) rather than
being decorative, which is what lets you read a panel at a glance from across
the room.
"""
from __future__ import annotations

PALETTE = {
    "bg":        "#0d1117",   # page
    "panel":     "#151b23",   # plot ground, a step above the page
    "panel_alt": "#1c232c",   # tiles
    "grid":      "#242c37",
    "border":    "#2b3441",
    "text":      "#e6edf3",
    "muted":     "#8b949e",
    "dim":       "#5a636d",

    # State colours. Used for status, never decoration.
    "ok":       "#3fb950",   # idle / healthy
    "active":   "#2dd4bf",   # motion / tracking
    "warn":     "#e3a008",   # calibrating
    "alert":    "#f85149",   # degraded / dead
    "info":     "#58a6ff",

    # Series colours, in the order they should be used.
    "s1": "#2dd4bf", "s2": "#58a6ff", "s3": "#bc8cff",
    "s4": "#e3a008", "s5": "#f778ba", "s6": "#7ee787",
}

SEQUENTIAL = "magma"      # waterfalls, spectrograms
FIELD = "viridis"         # reconstruction fields

MONO = ["DejaVu Sans Mono", "Liberation Mono", "monospace"]
SANS = ["DejaVu Sans", "Liberation Sans", "sans-serif"]


def apply() -> None:
    """Set global rcParams. Call once, before creating any figure."""
    import matplotlib as mpl

    p = PALETTE
    mpl.rcParams.update({
        "figure.facecolor": p["bg"],
        "figure.edgecolor": p["bg"],
        "savefig.facecolor": p["bg"],
        "axes.facecolor": p["panel"],
        "axes.edgecolor": p["border"],
        "axes.labelcolor": p["muted"],
        "axes.titlecolor": p["text"],
        # No box. Data does not need a frame; it needs a baseline.
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.spines.left": False,
        "axes.spines.bottom": False,
        "axes.grid": True,
        "axes.grid.axis": "both",
        "grid.color": p["grid"],
        "grid.linewidth": 0.6,
        "grid.alpha": 0.9,
        "axes.axisbelow": True,
        "xtick.color": p["dim"],
        "ytick.color": p["dim"],
        "xtick.labelsize": 7.5,
        "ytick.labelsize": 7.5,
        "xtick.major.size": 0,
        "ytick.major.size": 0,
        "text.color": p["text"],
        "font.family": "sans-serif",
        "font.sans-serif": SANS,
        "font.size": 9,
        "axes.titlesize": 9.5,
        "axes.titleweight": "bold",
        "axes.titlelocation": "left",
        "axes.titlepad": 8,
        "legend.frameon": False,
        "legend.fontsize": 7.5,
        "legend.labelcolor": p["muted"],
        "lines.linewidth": 1.3,
        "lines.solid_capstyle": "round",
        "figure.dpi": 110,
    })


def panel(ax, title: str = "", subtitle: str = "") -> None:
    """Left-aligned title with an optional dim subtitle beneath it.

    Two things this gets right that the obvious implementation does not:

    * Offsets are in POINTS, not axes fractions. An axes-fraction offset shrinks
      with the panel, so on a short panel the subtitle rides up into the title
      and the header reads as struck through.
    * The artists are created once and reused. Called from an animation loop, a
      naive version adds two Text objects per frame forever, which leaks memory
      and slowly darkens the header as identical glyphs stack up.
    """
    tt = getattr(ax, "_wf_title", None)
    # ax.clear() removes the artists but leaves this attribute pointing at the
    # dead ones, so setting their text silently does nothing and the header just
    # disappears. Membership in ax.texts is the real liveness test.
    if tt is not None and tt not in ax.texts:
        tt = None
    if tt is None:
        tt = ax.annotate("", xy=(0, 1), xycoords="axes fraction",
                         xytext=(0, 20), textcoords="offset points",
                         fontsize=9.5, color=PALETTE["text"], weight="bold",
                         va="bottom", ha="left", annotation_clip=False)
        st = ax.annotate("", xy=(0, 1), xycoords="axes fraction",
                         xytext=(0, 8), textcoords="offset points",
                         fontsize=7.5, color=PALETTE["dim"],
                         va="bottom", ha="left", annotation_clip=False)
        ax._wf_title, ax._wf_sub = tt, st
    ax._wf_title.set_text(title)
    ax._wf_sub.set_text(subtitle)


def image_panel(ax) -> None:
    """Prepare an axis that will hold an image: no grid over the pixels."""
    ax.grid(False)
    ax.set_facecolor(PALETTE["panel"])


def tile(ax, label: str, value: str, color: str | None = None,
         note: str = "") -> None:
    """Render one metric tile: big value, small label above, optional note.

    Tiles beat a monospace text dump because the eye can land on a number
    without parsing a sentence around it.
    """
    p = PALETTE
    ax.clear()
    ax.set_xticks([]); ax.set_yticks([])
    ax.set_facecolor(p["panel_alt"])
    ax.grid(False)
    for spine in ax.spines.values():
        spine.set_visible(True)
        spine.set_color(p["border"])
        spine.set_linewidth(0.8)

    # matplotlib Text has no letter-spacing property, so widen the label by
    # inserting thin spaces. Small-caps-with-tracking is what makes a tile label
    # read as a label rather than as more data.
    spaced = "\u2009".join(label.upper())
    ax.text(0.5, 0.78, spaced, ha="center", va="center", fontsize=6.5,
            color=p["dim"], transform=ax.transAxes)
    ax.text(0.5, 0.44, value, ha="center", va="center", fontsize=15,
            color=color or p["text"], transform=ax.transAxes,
            family=MONO, weight="bold")
    if note:
        ax.text(0.5, 0.14, note, ha="center", va="center", fontsize=6.5,
                color=p["muted"], transform=ax.transAxes)


class StatusBar:
    """The one big line that says what the system is doing right now."""

    def __init__(self, fig, y: float = 0.975):
        p = PALETTE
        self._text = fig.text(0.5, y, "", ha="center", va="center",
                              fontsize=13, weight="bold", color=p["muted"])
        self._sub = fig.text(0.5, y - 0.030, "", ha="center", va="center",
                             fontsize=8.5, color=p["dim"], family=MONO)

    def set(self, state: str, detail: str = "", kind: str = "muted") -> None:
        self._text.set_text(state)
        self._text.set_color(PALETTE.get(kind, PALETTE["muted"]))
        self._sub.set_text(detail)


def fade(color: str, alpha: float) -> tuple:
    """Blend a hex colour toward the panel ground, for de-emphasised marks."""
    import matplotlib.colors as mc
    fg = mc.to_rgb(color)
    bg = mc.to_rgb(PALETTE["panel"])
    return tuple(bg[i] + (fg[i] - bg[i]) * alpha for i in range(3))
