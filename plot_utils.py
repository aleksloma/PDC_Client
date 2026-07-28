# plot_utils.py — v2.0
"""Helpers to execute plotting code (Matplotlib, Seaborn, Plotly) and capture figures as base64 images."""
import io, base64, re, traceback
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd
import matplotlib.patches as mpatches

# Import visualization libraries
try:
    import seaborn as sns
    sns.set_theme()  # Set default seaborn theme
except ImportError:
    sns = None

# --- Georgian/Unicode font support ---
# Register DejaVuSans which supports Georgian (U+10A0-U+10FF) and other non-Latin scripts.
# MUST be after sns.set_theme() because seaborn resets matplotlib font settings.
from pathlib import Path as _Path

def _setup_unicode_font():
    """Register DejaVuSans for non-Latin text support in charts."""
    import logging
    try:
        from matplotlib import font_manager as _fm
        _here = _Path(__file__).resolve().parent
        candidates = [
            _here / "static" / "fonts" / "DejaVuSans.ttf",
            _Path("/app/static/fonts/DejaVuSans.ttf"),
        ]
        font_path = None
        for c in candidates:
            if c.exists():
                font_path = str(c)
                break

        if not font_path:
            logging.warning("DejaVuSans.ttf not found — Georgian text in charts will not render")
            return

        _fm.fontManager.addfont(font_path)
        matplotlib.rcParams['font.family'] = 'sans-serif'
        matplotlib.rcParams['font.sans-serif'] = ['DejaVu Sans'] + matplotlib.rcParams.get('font.sans-serif', [])
        logging.info(f"Unicode font registered: {font_path}")
    except Exception as e:
        logging.warning(f"Font registration failed (non-fatal): {e}")

_setup_unicode_font()

try:
    import plotly.graph_objects as go
    import plotly.express as px
    import plotly.io as pio
    from plotly.subplots import make_subplots
except ImportError:
    go = None
    px = None
    pio = None
    make_subplots = None

# Pin a non-opening Plotly renderer at module load. On a dev machine the
# auto-detected default is "browser" — which makes ANY fig.show() / pio.show()
# in LLM-generated plot code spin up a temporary HTTP server on a random high
# port and pop a browser tab when render_plot_safe() execs the code. The "json"
# renderer never opens a browser or a server. Charts are always captured via
# _plotly_to_html(); show() plays no part in capture, so this is purely defensive.
if pio is not None:
    try:
        pio.renderers.default = "json"
    except Exception:
        pass

# --- Extended visualization libraries ---

# Venn diagrams
try:
    from matplotlib_venn import venn2, venn3, venn2_circles, venn3_circles
    _HAS_VENN = True
except ImportError:
    venn2 = venn3 = venn2_circles = venn3_circles = None
    _HAS_VENN = False

# Word clouds
try:
    from wordcloud import WordCloud
    _HAS_WORDCLOUD = True
except ImportError:
    WordCloud = None
    _HAS_WORDCLOUD = False

# Network graphs
try:
    import networkx as nx
    _HAS_NETWORKX = True
except ImportError:
    nx = None
    _HAS_NETWORKX = False

# Treemaps
try:
    import squarify
    _HAS_SQUARIFY = True
except ImportError:
    squarify = None
    _HAS_SQUARIFY = False

# Statistics
try:
    from scipy import stats as scipy_stats
    _HAS_SCIPY = True
except ImportError:
    scipy_stats = None
    _HAS_SCIPY = False

# Missing data visualization
try:
    import missingno as msno
    _HAS_MISSINGNO = True
except ImportError:
    msno = None
    _HAS_MISSINGNO = False

# Calendar heatmaps
try:
    import calplot
    _HAS_CALPLOT = True
except ImportError:
    calplot = None
    _HAS_CALPLOT = False

# UpSet plots (multi-set intersections — use for 4+ sets instead of Venn)
try:
    from upsetplot import UpSet, from_memberships, from_contents
    _HAS_UPSETPLOT = True
except ImportError:
    UpSet = None
    from_memberships = None
    from_contents = None
    _HAS_UPSETPLOT = False

# Text label adjustment
try:
    from adjustText import adjust_text
    _HAS_ADJUSTTEXT = True
except ImportError:
    adjust_text = None
    _HAS_ADJUSTTEXT = False

# Machine Learning libraries
try:
    from sklearn.model_selection import train_test_split
    from sklearn.linear_model import LogisticRegression
    from sklearn.tree import DecisionTreeClassifier, plot_tree as sklearn_plot_tree
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.cluster import KMeans as sklearn_KMeans
    from sklearn.preprocessing import StandardScaler, LabelEncoder as sklearn_LabelEncoder
    from sklearn.metrics import (
        accuracy_score, classification_report, confusion_matrix,
        roc_curve, auc as sklearn_auc, silhouette_score
    )
    _HAS_SKLEARN = True
except ImportError:
    train_test_split = None
    LogisticRegression = None
    DecisionTreeClassifier = None
    sklearn_plot_tree = None
    RandomForestClassifier = None
    sklearn_KMeans = None
    StandardScaler = None
    sklearn_LabelEncoder = None
    accuracy_score = None
    classification_report = None
    confusion_matrix = None
    roc_curve = None
    sklearn_auc = None
    silhouette_score = None
    _HAS_SKLEARN = False

def upset_plot_from_sets(sets_dict, figsize=(14, 8), max_intersections=30, title='Set Intersections'):
    """Create an UpSet-style intersection plot using pure matplotlib.

    Shows which sets overlap and by how much — the standard way to visualize
    intersections when there are more sets than Venn diagrams can handle.

    Args:
        sets_dict: Dict of {set_name: set_of_items}
        figsize: Figure size tuple
        max_intersections: Max number of intersection bars to show (sorted by size)
        title: Plot title

    Returns:
        matplotlib Figure object
    """
    set_names = sorted(sets_dict.keys())
    n_sets = len(set_names)

    # Calculate all intersections
    all_items = set().union(*sets_dict.values())
    membership = {}
    for item in all_items:
        key = tuple(item in sets_dict[name] for name in set_names)
        membership.setdefault(key, 0)
        membership[key] += 1

    # Sort by size descending, limit
    sorted_ints = sorted(membership.items(), key=lambda x: x[1], reverse=True)[:max_intersections]
    n_ints = len(sorted_ints)
    keys = [k for k, v in sorted_ints]
    counts = [v for k, v in sorted_ints]
    set_sizes = [len(sets_dict[name]) for name in set_names]

    # Create figure with gridspec
    fig = plt.figure(figsize=figsize)
    fig.patch.set_facecolor('white')
    gs = fig.add_gridspec(2, 2, width_ratios=[1, 4], height_ratios=[3, 1], hspace=0.05, wspace=0.05)

    # Top-right: Intersection size bars
    ax_bars = fig.add_subplot(gs[0, 1])
    bar_objs = ax_bars.bar(range(n_ints), counts, color='#2b2b2b', width=0.6, edgecolor='none')
    for b, c in zip(bar_objs, counts):
        ax_bars.text(b.get_x() + b.get_width() / 2, b.get_height(), f'{c:,}',
                     ha='center', va='bottom', fontsize=8)
    ax_bars.set_ylabel('Intersection Size', fontsize=10)
    ax_bars.set_xlim(-0.5, n_ints - 0.5)
    ax_bars.set_xticks([])
    for spine in ['top', 'right', 'bottom']:
        ax_bars.spines[spine].set_visible(False)
    ax_bars.set_facecolor('white')

    # Bottom-right: Dot matrix
    ax_dots = fig.add_subplot(gs[1, 1])
    for i, key in enumerate(keys):
        active = [j for j, v in enumerate(key) if v]
        inactive = [j for j, v in enumerate(key) if not v]
        for j in inactive:
            ax_dots.scatter(i, j, color='#e0e0e0', s=80, zorder=3)
        for j in active:
            ax_dots.scatter(i, j, color='#2b2b2b', s=80, zorder=3)
        if len(active) > 1:
            ax_dots.plot([i, i], [min(active), max(active)], color='#2b2b2b', linewidth=2, zorder=2)
    ax_dots.set_yticks(range(n_sets))
    ax_dots.set_yticklabels(set_names, fontsize=10)
    ax_dots.set_xlim(-0.5, n_ints - 0.5)
    ax_dots.set_ylim(-0.5, n_sets - 0.5)
    ax_dots.set_xticks([])
    ax_dots.invert_yaxis()
    for spine in ax_dots.spines.values():
        spine.set_visible(False)
    ax_dots.set_facecolor('white')
    ax_dots.grid(axis='y', linestyle='-', alpha=0.1)

    # Bottom-left: Set size bars (horizontal)
    ax_sizes = fig.add_subplot(gs[1, 0])
    h_bars = ax_sizes.barh(range(n_sets), set_sizes, color='#4C72B0', height=0.6, edgecolor='none')
    for b, s in zip(h_bars, set_sizes):
        ax_sizes.text(b.get_width(), b.get_y() + b.get_height() / 2, f' {s:,}',
                      ha='left', va='center', fontsize=9)
    ax_sizes.set_yticks(range(n_sets))
    ax_sizes.set_yticklabels(set_names, fontsize=10)
    ax_sizes.invert_xaxis()
    ax_sizes.invert_yaxis()
    ax_sizes.set_ylim(-0.5, n_sets - 0.5)
    ax_sizes.set_xlabel('Set Size', fontsize=10)
    ax_sizes.spines['top'].set_visible(False)
    ax_sizes.spines['right'].set_visible(False)
    ax_sizes.set_facecolor('white')

    # Top-left: empty
    ax_empty = fig.add_subplot(gs[0, 0])
    ax_empty.axis('off')

    fig.suptitle(title, fontsize=14, y=1.02)
    return fig


GLOBAL_PLOT_SCOPE = {
    # Core
    "pd": pd,
    "np": np,
    "plt": plt,
    "mticker": mticker,
    "mpatches": mpatches,
    "sns": sns,
    "go": go,
    "px": px,
    "make_subplots": make_subplots,
    # Venn diagrams
    "venn2": venn2,
    "venn3": venn3,
    "venn2_circles": venn2_circles,
    "venn3_circles": venn3_circles,
    # Extended visualization
    "WordCloud": WordCloud,
    "nx": nx,
    "squarify": squarify,
    "scipy_stats": scipy_stats,
    "msno": msno,
    "calplot": calplot,
    "upset_plot_from_sets": upset_plot_from_sets,
    "adjust_text": adjust_text,
    # Machine Learning
    "train_test_split": train_test_split,
    "LogisticRegression": LogisticRegression,
    "DecisionTreeClassifier": DecisionTreeClassifier,
    "plot_tree": sklearn_plot_tree,
    "RandomForestClassifier": RandomForestClassifier,
    "KMeans": sklearn_KMeans,
    "StandardScaler": StandardScaler,
    "LabelEncoder": sklearn_LabelEncoder,
    "accuracy_score": accuracy_score,
    "classification_report": classification_report,
    "confusion_matrix": confusion_matrix,
    "roc_curve": roc_curve,
    "auc": sklearn_auc,
    "silhouette_score": silhouette_score,
}

def _bar_data_value(patch) -> float:
    """Get the data value from a bar patch, handling both vertical and horizontal bars.

    Vertical bars (plt.bar): data is in get_height(), width is the bar thickness (~0.8).
    Horizontal bars (plt.barh): data is in get_width(), height is the bar thickness (~0.8).
    We detect orientation by checking which dimension looks like a "thickness" constant.
    """
    h = patch.get_height() if hasattr(patch, 'get_height') else None
    w = patch.get_width() if hasattr(patch, 'get_width') else None
    if h is None and w is None:
        return 0.0
    if h is None:
        return float(w) if w is not None else 0.0
    if w is None:
        return float(h)
    # For horizontal bars, height is thickness (~0.8) and width is data value
    # For vertical bars, width is thickness (~0.8) and height is data value
    # Heuristic: if height looks like a uniform thickness (0 < h < 1) and width varies, it's horizontal
    # We can't be 100% sure from a single patch, so we check the x position too
    # Simpler heuristic: barh patches have x=0 (start from left axis)
    x = patch.get_x() if hasattr(patch, 'get_x') else 0
    y = patch.get_y() if hasattr(patch, 'get_y') else 0
    # For barh: x is 0 (or data start), y varies; data = width
    # For bar:  y is 0 (or data start), x varies; data = height
    if abs(x) < 1e-9 and abs(y) > 1e-9:
        return float(w)  # horizontal bar
    return float(h)  # vertical bar (default)


def _encode_current_figure() -> str:
    """Encode the current Matplotlib/Seaborn figure to base64 PNG and close it."""
    import logging
    buf = io.BytesIO()
    fig = plt.gcf()
    # Thousands-group numeric axis ticks + data labels before layout/save.
    # Guarded; a formatting failure must never block the image (Article IV).
    _format_matplotlib_numeric(fig)
    try:
        fig.tight_layout()
    except Exception as e:
        # tight_layout() can fail due to invalid format strings in tick labels
        # (e.g. FormatStrFormatter('%,d') — '%,' is not a valid format specifier).
        # bbox_inches="tight" in savefig handles layout as a fallback.
        logging.warning(f"tight_layout() failed (non-fatal, using bbox_inches fallback): {e}")
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")

def _encode_plotly_figure(fig) -> str:
    """Convert Plotly figure to base64 PNG using kaleido."""
    try:
        # Export as PNG bytes
        img_bytes = fig.to_image(format="png", width=1200, height=800, scale=2)
        return base64.b64encode(img_bytes).decode("ascii")
    except Exception as e:
        # Fallback: try to get matplotlib figure if available
        raise Exception(f"Plotly image export failed: {e}. Make sure 'kaleido' is installed.")

def _wide_color_sequence(n: int) -> list:
    """A palette of n visually-distinct colors. Dark24+Light24 (48 unique)
    covers most cases; beyond that, generate n evenly-spaced HSL hues so no
    two groups ever collide."""
    wide = []
    try:
        import plotly.express as _px
        seen = set()
        for c in list(_px.colors.qualitative.Dark24) + list(_px.colors.qualitative.Light24):
            if c not in seen:
                seen.add(c)
                wide.append(c)
    except Exception:
        wide = []
    if n <= len(wide):
        return wide[:n]
    import colorsys
    out = []
    for i in range(n):
        r, g, b = colorsys.hls_to_rgb((i / max(n, 1)), 0.5, 0.6)
        out.append('#%02x%02x%02x' % (int(r * 255), int(g * 255), int(b * 255)))
    return out


def _widen_discrete_colors(fig) -> None:
    """When a categorical (discrete-color) chart has more than ~10 color groups,
    Plotly's default 10-color sequence repeats hues so distinct categories look
    identical. Reassign each group a distinct color from a wide sequence.

    Group-count-based and generic. GUARDS (never touched):
      - continuous color scales / colorbars (a `coloraxis` with a colorscale),
      - a 2nd-measure numeric color (marker/line color is an array, not a single
        color string),
      - charts with <= 10 color groups (Plotly's default is already fine).
    """
    try:
        # A continuous color axis (e.g. a dual-measure colorbar) → leave alone.
        try:
            ca = fig.layout.coloraxis
            if ca is not None and getattr(ca, 'colorscale', None):
                return
        except Exception:
            pass

        colored = []  # traces whose color is a single discrete color string
        for tr in fig.data:
            marker = getattr(tr, 'marker', None)
            line = getattr(tr, 'line', None)
            mcol = getattr(marker, 'color', None) if marker is not None else None
            lcol = getattr(line, 'color', None) if line is not None else None
            # A per-point / continuous color is array-like → bail entirely so we
            # never disturb a numeric color measure or colorbar.
            for col in (mcol, lcol):
                if col is not None and not isinstance(col, str) and hasattr(col, '__len__'):
                    return
            colored.append(tr)

        if len(colored) <= 10:
            return

        palette = _wide_color_sequence(len(colored))
        for i, tr in enumerate(colored):
            c = palette[i]
            try:
                if getattr(tr, 'marker', None) is not None:
                    tr.marker.color = c
            except Exception:
                pass
            try:
                if getattr(tr, 'line', None) is not None and getattr(tr.line, 'color', None):
                    tr.line.color = c
            except Exception:
                pass
    except Exception:
        # Cosmetic only — never block the render (Article IV).
        pass


def _plotly_to_html(fig) -> str:
    """Convert Plotly figure to interactive HTML string with full-height styling."""
    try:
        # Update figure layout to use autosize and fill container.
        # title.automargin: True lets Plotly grow the top margin to fit the title text
        # so the final character is never clipped on narrow viewports.
        # Smaller default margins give the chart area more room on phone widths.
        fig.update_layout(
            autosize=True,
            height=None,
            width=None,
            margin=dict(l=50, r=20, t=50, b=40),
        )
        # Apply title automargin only if a title text already exists, so we don't
        # accidentally inject an empty title slot for chart types that have none.
        try:
            existing_title = fig.layout.title
            if existing_title and existing_title.text:
                fig.update_layout(title=dict(
                    text=existing_title.text,
                    x=0.5,
                    xanchor='center',
                    automargin=True,
                ))
        except Exception:
            pass

        # Thousands-group numeric axes + numeric data labels (display polish).
        # Guarded internally; never blocks the chart render.
        _format_plotly_numeric(fig)

        # Conditionally switch a value axis to log when its positive range spans
        # many orders of magnitude (a few extremes compress the bulk). Guarded
        # internally; only fires for genuinely extreme, all-positive ranges.
        _maybe_log_plotly_axes(fig)

        # Widen the discrete color palette for high-cardinality categorical
        # charts so > ~10 groups never share a repeated hue. Guarded internally;
        # continuous/2nd-measure color scales and <= 10-group charts untouched.
        _widen_discrete_colors(fig)

        # Generate standalone HTML with responsive sizing
        html = fig.to_html(
            include_plotlyjs='cdn',
            config={
                'displayModeBar': True,
                'displaylogo': False,
                # Box/Lasso Select do nothing useful on these analytics charts;
                # drop them. Zoom/pan/zoomin/zoomout/autoScale2d/resetScale2d stay.
                'modeBarButtonsToRemove': ['toImage', 'sendDataToCloud', 'select2d', 'lasso2d'],
                'responsive': True
            },
            div_id='plotly-chart'
        )
        
        # Inject CSS and JS to make plot fill full height and auto-resize
        full_height_style = """
        <style>
            html, body {
                margin: 0;
                padding: 0;
                width: 100%;
                height: 100%;
                overflow: hidden;
            }
            #plotly-chart {
                width: 100% !important;
                height: 100% !important;
                position: absolute;
                top: 0;
                left: 0;
                right: 0;
                bottom: 0;
            }
            .plotly-graph-div {
                width: 100% !important;
                height: 100% !important;
            }
            .js-plotly-plot {
                width: 100% !important;
                height: 100% !important;
            }
        </style>
        <script>
            window.addEventListener('load', function() {
                // Force Plotly to resize to fill container
                setTimeout(function() {
                    var plotDiv = document.getElementById('plotly-chart');
                    if (plotDiv && window.Plotly) {
                        window.Plotly.Plots.resize(plotDiv);
                    }
                }, 100);
            });
            
            // Handle window resize
            window.addEventListener('resize', function() {
                var plotDiv = document.getElementById('plotly-chart');
                if (plotDiv && window.Plotly) {
                    window.Plotly.Plots.resize(plotDiv);
                }
            });
        </script>
        """
        
        # Insert style and script right after <head> tag
        html = html.replace('<head>', '<head>' + full_height_style)
        
        return html
    except Exception as e:
        raise Exception(f"Plotly HTML export failed: {e}")

def _chart_scalar(v):
    """Coerce a single plotted value to a JSON-safe scalar (numpy/pandas → native)."""
    try:
        if v is None or isinstance(v, (str, bool, int, float)):
            return v
        if isinstance(v, np.generic):
            return v.item()
        return str(v)
    except Exception:
        return str(v)


def _plotly_layout_axis(fig, ref):
    """Resolve a trace axis ref like 'x', 'y2' to its layout axis object."""
    try:
        if not ref:
            return None
        letter, num = ref[0], ref[1:]
        name = ("xaxis" if letter == "x" else "yaxis") + (num if num else "")
        return getattr(getattr(fig, "layout", None), name, None)
    except Exception:
        return None


def _plotly_axis_title(axis):
    try:
        t = getattr(axis, "title", None)
        return (getattr(t, "text", None) if t is not None else None) or None
    except Exception:
        return None


def _plotly_traces_to_table(fig, traces, xref="x", yref="y"):
    """Build one {title, columns, rows, total_rows} table from a set of Plotly
    traces sharing a subplot. None if no tabular data. Never raises."""
    try:
        if not traces:
            return None
        x_title = _plotly_axis_title(_plotly_layout_axis(fig, xref)) or "x"
        y_title = _plotly_axis_title(_plotly_layout_axis(fig, yref)) or "y"
        multi = len(traces) > 1
        rows = []
        for i, tr in enumerate(traces):
            name = getattr(tr, "name", None) or f"series {i + 1}"
            labels = getattr(tr, "labels", None)
            values = getattr(tr, "values", None)
            if labels is not None and values is not None:
                for lab, val in zip(list(labels), list(values)):
                    row = {"Label": _chart_scalar(lab), "Value": _chart_scalar(val)}
                    if multi:
                        row = {"Series": name, **row}
                    rows.append(row)
                continue
            xs = getattr(tr, "x", None)
            ys = getattr(tr, "y", None)
            if xs is None and ys is None:
                continue
            xs_list = list(xs) if xs is not None else []
            ys_list = list(ys) if ys is not None else []
            for j in range(max(len(xs_list), len(ys_list))):
                xv = xs_list[j] if j < len(xs_list) else None
                yv = ys_list[j] if j < len(ys_list) else None
                row = {x_title: _chart_scalar(xv), y_title: _chart_scalar(yv)}
                if multi:
                    row = {"Series": name, **row}
                rows.append(row)
        if not rows:
            return None
        # The y-axis title (the measure) is the most useful subplot label.
        title = _plotly_axis_title(_plotly_layout_axis(fig, yref)) \
            or _plotly_axis_title(_plotly_layout_axis(fig, xref)) or None
        return {"title": title, "columns": list(rows[0].keys()),
                "rows": rows[:5000], "total_rows": len(rows)}
    except Exception:
        return None


def _extract_plotly_chart_data(fig):
    """Extract the data a Plotly figure was built from for the client-side
    'Show data' button. A single subplot returns {columns, rows, total_rows};
    a make_subplots figure with multiple axis pairs returns
    {"tables": [{title, columns, rows, total_rows}, ...]}.

    Returns None on any failure — display-only, must NEVER break chart rendering
    (Article IV). No raw data leaves the client (rides the client's own
    SSE/full_table path back to the browser).
    """
    try:
        traces = list(getattr(fig, "data", None) or [])
        if not traces:
            return None

        # Group traces by their subplot axis pair (xaxis/yaxis refs).
        groups = {}
        order = []
        for tr in traces:
            key = (getattr(tr, "xaxis", None) or "x", getattr(tr, "yaxis", None) or "y")
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(tr)

        if len(order) > 1:
            tables = []
            for idx, key in enumerate(order):
                t = _plotly_traces_to_table(fig, groups[key], key[0], key[1])
                if t:
                    if not t.get("title"):
                        t["title"] = f"Chart {idx + 1}"
                    tables.append(t)
            if len(tables) > 1:
                return {"tables": tables}
            if len(tables) == 1:
                t = tables[0]
                return {"columns": t["columns"], "rows": t["rows"], "total_rows": t["total_rows"]}
            return None

        # Single subplot → flat single table (back-compat shape).
        key = order[0] if order else ("x", "y")
        t = _plotly_traces_to_table(fig, traces, key[0], key[1])
        if not t:
            return None
        return {"columns": t["columns"], "rows": t["rows"], "total_rows": t["total_rows"]}
    except Exception:
        return None


def _is_plotly_grid(fig):
    """True if a Plotly figure is a multi-CELL subplot grid — make_subplots with
    rows>1 or cols>1, or a faceted small-multiple: two+ charts crammed into one
    image. A TRUE dual-axis (secondary_y) has ONE x-domain and a y2 axis that
    OVERLAYS the primary y (same cell) — that is NOT a grid and returns False so
    it renders as a single chart. Never raises (Article IV)."""
    try:
        layout = fig.to_dict().get("layout", {}) or {}
        xdoms, ydoms = set(), set()
        for k, v in layout.items():
            if not isinstance(v, dict):
                continue
            # Overlaying axes (secondary_y / twin) share a cell — never a new cell.
            if v.get("overlaying"):
                continue
            d = v.get("domain")
            if not d:
                continue
            try:
                dom = (round(float(d[0]), 3), round(float(d[1]), 3))
            except Exception:
                continue
            if k.startswith("xaxis"):
                xdoms.add(dom)
            elif k.startswith("yaxis"):
                ydoms.add(dom)
        # >1 distinct x-domain (columns) or >1 distinct y-domain (rows) → grid.
        return len(xdoms) > 1 or len(ydoms) > 1
    except Exception:
        return False


def _split_plotly_grid(fig):
    """Split a Plotly multi-cell subplot grid into one standalone figure per
    cell, so the user never sees two charts in one image. Returns a list of
    {image (HTML), is_plotly, title, chart_data}; [] if it cannot split. The
    fallback path only — the preferred fix is regeneration as separate
    ###NEXT_PLOT### blocks. Never raises."""
    if go is None:
        return []
    try:
        traces = list(getattr(fig, "data", None) or [])
        if not traces:
            return []
        # Group traces by their (xaxis, yaxis) anchor — each cell is one group.
        groups, order = {}, []
        for tr in traces:
            key = (getattr(tr, "xaxis", None) or "x", getattr(tr, "yaxis", None) or "y")
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(tr)
        if len(order) <= 1:
            return []
        ann = list(getattr(getattr(fig, "layout", None), "annotations", None) or [])
        charts = []
        for idx, key in enumerate(order):
            sub = go.Figure()
            for tr in groups[key]:
                d = tr.to_plotly_json()
                d.pop("xaxis", None)   # re-anchor to the default x/y of the new fig
                d.pop("yaxis", None)
                sub.add_trace(d)
            xt = _plotly_axis_title(_plotly_layout_axis(fig, key[0]))
            yt = _plotly_axis_title(_plotly_layout_axis(fig, key[1]))
            sub.update_layout(template="plotly_white")
            if xt:
                sub.update_xaxes(title_text=xt)
            if yt:
                sub.update_yaxes(title_text=yt)
            # Best-effort per-cell title from the grid's subplot_titles annotations.
            title = None
            if idx < len(ann):
                title = (getattr(ann[idx], "text", None) or "").strip() or None
            if title:
                sub.update_layout(title=dict(text=title, x=0.5, xanchor="center"))
            try:
                html = _plotly_to_html(sub)
            except Exception:
                html = None
            if not html:
                continue
            charts.append({
                "image": html, "is_plotly": True, "title": title,
                "chart_data": _extract_plotly_chart_data(sub),
            })
        return charts
    except Exception:
        return []


# Label HEAD words that mark an axis/column as a year/identifier DIMENSION whose
# numeric values must NOT be comma-grouped (EN + Georgian). We match the HEAD
# (last) word only — not any substring — so a MEASURE label that merely mentions
# one of these words still groups: "Order Value" / "Number of Orders" /
# "Reference Price" / "ID Count" group; "Order ID" / "Customer Code" / "Year"
# stay raw. Mirrors the frontend `_isIdentifierLabel`.
_IDENTIFIER_HEAD_WORDS = frozenset({
    "year", "yr", "წელი", "id", "ids", "code", "codes", "კოდი", "იდ",
    "zip", "postal", "phone", "account", "iban", "invoice", "order", "ref",
})


def _is_identifier_label(label) -> bool:
    """True when the label's HEAD (last) word names a year/identifier dimension,
    so its numeric values should stay un-grouped. Never raises."""
    try:
        if not label:
            return False
        # Unicode letter tokens (drops digits/punctuation/units like "(USD)").
        toks = re.findall(r"[^\W\d_]+", str(label))
        if not toks:
            return False
        return toks[-1].lower() in _IDENTIFIER_HEAD_WORDS
    except Exception:
        return False


def _df_like_to_chart_table(obj):
    """Convert a pandas DataFrame/Series to a {columns, rows, total_rows} table,
    or None. A non-default index is reset so a groupby key surfaces as a column."""
    try:
        if obj is None:
            return None
        if isinstance(obj, pd.Series):
            obj = obj.reset_index()
        if not isinstance(obj, pd.DataFrame):
            return None
        if obj.shape[0] < 1 or obj.shape[1] < 1:
            return None
        if not isinstance(obj.index, pd.RangeIndex):
            try:
                obj = obj.reset_index()
            except Exception:
                pass
        _MAX = 5000
        cols = [str(c) for c in obj.columns]
        rows = []
        for _, r in obj.head(_MAX).iterrows():
            rows.append({c: _chart_scalar(v) for c, v in zip(cols, r.tolist())})
        if not rows:
            return None
        return {"columns": cols, "rows": rows, "total_rows": int(obj.shape[0])}
    except Exception:
        return None


# Standard matplotlib/seaborn chart calls that SHOULD be interactive Plotly
# instead. Explicit seaborn function list so the genuine exceptions
# (kdeplot/pairplot/jointplot — no Plotly equivalent) are NOT matched.
_MPL_STANDARD_CHART_RE = re.compile(
    r'\bsns\.(?:barplot|countplot|lineplot|scatterplot|histplot|boxplot|violinplot|'
    r'pointplot|stripplot|swarmplot|heatmap|regplot|catplot|relplot|boxenplot|ecdfplot)\b'
    r'|\b(?:plt|ax|ax1|ax2|axes)\.(?:bar|barh|plot|scatter|pie|hist|boxplot|violinplot|stackplot|fill_between|stem|step)\b',
    re.IGNORECASE,
)
# Genuine matplotlib-only visualizations — these MUST stay matplotlib and must
# NEVER be regenerated as Plotly.
_MPL_EXCEPTION_RE = re.compile(
    r'\bplot_tree\b|\bvenn2\b|\bvenn3\b|\bvenn2_circles\b|\bvenn3_circles\b'
    r'|\bupset_plot_from_sets\b|\bUpSet\b|\bWordCloud\b|\bnetworkx\b|\bnx\.'
    r'|\bmsno\b|\bmissingno\b|\bcalplot\b|\bsquarify\b'
    r'|\bsns\.(?:kdeplot|pairplot|jointplot|displot|PairGrid|JointGrid)\b',
    re.IGNORECASE,
)


def _is_noninteractive_standard_chart(code: str) -> bool:
    """True when generated plot code renders a STANDARD chart with
    matplotlib/seaborn (which should be interactive Plotly) and does NOT use any
    genuine matplotlib-only exception (plot_tree/venn/upset/wordcloud/networkx/
    missingno/calplot/squarify/KDE/pair). Used by the client-side interactivity
    guard to trigger a one-shot Plotly regeneration. Never raises."""
    try:
        if not code:
            return False
        if _MPL_EXCEPTION_RE.search(code):
            return False
        return bool(_MPL_STANDARD_CHART_RE.search(code))
    except Exception:
        return False


def _mpl_axis_has_plot(ax):
    """True if a matplotlib axis carries plotted bars or real lines."""
    try:
        if getattr(ax, "containers", None):
            return True
        return any(hasattr(ln, "get_xdata") and len(ln.get_xdata()) > 2
                   for ln in ax.get_lines())
    except Exception:
        return False


def _bars_are_horizontal(bars):
    """True if a set of bar patches are HORIZONTAL (categories on the Y axis,
    value on X). Horizontal bars share a constant height (thickness) and vary in
    width; vertical bars share a constant width and vary in height."""
    try:
        widths = [round(float(p.get_width()), 6) for p in bars if hasattr(p, "get_width")]
        heights = [round(float(p.get_height()), 6) for p in bars if hasattr(p, "get_height")]
        if not widths or not heights:
            return False
        return len(set(heights)) <= 1 and len(set(widths)) > 1
    except Exception:
        return False


def _bar_category_label(p, horizontal, cats):
    """Map a bar to its category tick label by its CENTER position on the
    category axis (robust for hue / multiple containers, where bar index does
    NOT equal category index). Returns None if out of range — never a tick
    position number."""
    try:
        center = (p.get_y() + p.get_height() / 2.0) if horizontal \
            else (p.get_x() + p.get_width() / 2.0)
        i = int(round(center))
        if 0 <= i < len(cats):
            return cats[i]
    except Exception:
        pass
    return None


def _mpl_axis_artist_table(ax):
    """Extract a {title, columns, rows, total_rows} table from ONE matplotlib
    axis via its plotted artists (bars then lines), or None. Never raises.

    Orientation-aware: HORIZONTAL bars read categories from the Y tick labels,
    VERTICAL bars from the X tick labels. Categories are matched to bars by
    position (so hue / multiple containers are not mislabeled or duplicated),
    and axis tick *positions* are never emitted as data.
    """
    try:
        title = (ax.get_title() or "").strip() or None
        bar_containers = [c for c in (getattr(ax, "containers", None) or [])
                          if len(c) > 0 and any(hasattr(p, "get_height") for p in c)]
        if bar_containers:
            all_bars = [p for c in bar_containers for p in c if hasattr(p, "get_height")]
            horizontal = _bars_are_horizontal(all_bars)
            cats = [t.get_text() for t in
                    (ax.get_yticklabels() if horizontal else ax.get_xticklabels())]
            multi = len(bar_containers) > 1
            rows = []
            for c in bar_containers:
                series = (str(c.get_label()) if c.get_label() is not None else "").strip()
                if series.startswith("_"):
                    series = ""
                for p in c:
                    if not hasattr(p, "get_height"):
                        continue
                    cat = _bar_category_label(p, horizontal, cats)
                    if cat in (None, "", "nan"):
                        continue
                    val = _bar_data_value(p)
                    # With hue there is one (absent) near-zero bar per category
                    # the series doesn't belong to — drop those so categories
                    # aren't duplicated with meaningless zeros.
                    if multi and abs(float(val)) < 1e-12:
                        continue
                    row = {}
                    if multi and series:
                        row["series"] = series
                    row["category"] = _chart_scalar(cat)
                    row["value"] = _chart_scalar(val)
                    rows.append(row)
            if rows:
                return {"title": title, "columns": list(rows[0].keys()),
                        "rows": rows[:5000], "total_rows": len(rows)}
        # Line charts: x / y per line (exclude grid/axis lines with <=2 pts).
        lines = [ln for ln in ax.get_lines()
                 if hasattr(ln, "get_xdata") and len(ln.get_xdata()) > 2]
        if lines:
            rows = []
            multi = len(lines) > 1
            for li, ln in enumerate(lines):
                name = ln.get_label() or f"series {li + 1}"
                if isinstance(name, str) and name.startswith("_"):
                    name = f"series {li + 1}"
                for x, y in zip(list(ln.get_xdata()), list(ln.get_ydata())):
                    row = {"x": _chart_scalar(x), "y": _chart_scalar(y)}
                    if multi:
                        row = {"series": name, **row}
                    rows.append(row)
            if rows:
                return {"title": title, "columns": list(rows[0].keys()),
                        "rows": rows[:5000], "total_rows": len(rows)}
        return None
    except Exception:
        return None


def _real_plot_axes(fig):
    """Genuine plotting axes of a matplotlib figure — excludes colorbars and
    twin/secondary axes that share another axis's position (counted once)."""
    out, seen = [], set()
    try:
        for a in fig.get_axes():
            try:
                if a.get_label() == "<colorbar>":
                    continue
                pos = a.get_position()
                key = (round(pos.x0, 2), round(pos.y0, 2), round(pos.x1, 2), round(pos.y1, 2))
                if key in seen:            # twinx/twiny share the same box
                    continue
                seen.add(key)
                out.append(a)
            except Exception:
                continue
    except Exception:
        pass
    return out


def _axes_single_row_or_col(axes):
    """True if >=2 real axes are laid out in a single ROW or single COLUMN — the
    classic plt.subplots(1,N)/(N,1) 'two charts in one image' mistake. NxM grids
    (e.g. pair plots, 2x2) return False so legitimate matrix viz is left intact."""
    try:
        if len(axes) < 2:
            return False
        pos = [a.get_position() for a in axes]
        ys = [round((p.y0 + p.y1) / 2.0, 2) for p in pos]
        xs = [round((p.x0 + p.x1) / 2.0, 2) for p in pos]
        single_row = len(set(ys)) == 1 and len(set(xs)) == len(xs)
        single_col = len(set(xs)) == 1 and len(set(ys)) == len(ys)
        return single_row or single_col
    except Exception:
        return False


def _split_figure_by_axis(fig, axes):
    """Render each axis as its own cropped PNG (base64), so the user never sees
    multiple plots in one image. Returns a list aligned with `axes` (None for any
    axis that fails). Fallback path only."""
    imgs = []
    try:
        fig.canvas.draw()
        renderer = fig.canvas.get_renderer()
    except Exception:
        return [None] * len(axes)
    for a in axes:
        try:
            bbox = a.get_tightbbox(renderer).transformed(fig.dpi_scale_trans.inverted())
            bbox = bbox.expanded(1.08, 1.10)
            buf = io.BytesIO()
            fig.savefig(buf, format="png", dpi=150, bbox_inches=bbox)
            imgs.append(base64.b64encode(buf.getvalue()).decode("ascii"))
        except Exception:
            imgs.append(None)
    return imgs


def _extract_matplotlib_chart_data(fig, env, dfs):
    """Best-effort table(s) of the data behind a matplotlib/seaborn figure for
    the client-side 'Show data' button.

    Display-only; never raises (Article IV). No raw data leaves the client —
    the result rides the client's own SSE/full_table path back to the browser.

    Multi-subplot figures (e.g. plt.subplots(1,2)) return one table PER plotted
    axis: {"tables": [{title, columns, rows, total_rows}, ...]}. Single-axis
    figures keep the flat {columns, rows, total_rows} shape and prefer the
    aggregated DataFrame the generated code built. Non-tabular viz (wordcloud /
    venn / network) → None (no button).
    """
    try:
        # Dedup twin/secondary axes by position so a dual-axis (twinx) combo —
        # ONE visual chart drawn on two overlaid axes — is treated as single and
        # gets ONE table from the source frame, NOT two artist tables.
        plotted_axes = [ax for ax in _real_plot_axes(fig) if _mpl_axis_has_plot(ax)]

        # Multi-subplot → one table per plotted axis. Use ARTIST extraction only:
        # the namespace DataFrame can't be mapped to a specific subplot.
        if len(plotted_axes) > 1:
            tables = []
            for idx, ax in enumerate(plotted_axes):
                t = _mpl_axis_artist_table(ax)
                if t:
                    if not t.get("title"):
                        t["title"] = f"Chart {idx + 1}"
                    tables.append(t)
            if len(tables) > 1:
                return {"tables": tables}
            if len(tables) == 1:
                t = tables[0]
                return {"columns": t["columns"], "rows": t["rows"], "total_rows": t["total_rows"]}
            # else fall through to the single-table heuristics below

        # (a) Single subplot — prefer an aggregated DataFrame from the exec
        # namespace that is NOT one of the raw source frames (compared by id).
        raw_ids = {id(v) for v in (dfs or {}).values()}
        best = None
        best_key = None
        for _name, obj in (env or {}).items():
            if not isinstance(obj, (pd.DataFrame, pd.Series)):
                continue
            if id(obj) in raw_ids:
                continue
            try:
                nrows = int(obj.shape[0])
            except Exception:
                continue
            if nrows < 1:
                continue
            is_df = isinstance(obj, pd.DataFrame)
            small = 1 <= nrows <= 200
            key = (1 if is_df else 0, 1 if small else 0)
            # `>=` so a later definition wins ties — the final aggregation that
            # actually gets plotted tends to be assigned last in the code.
            if best is None or key >= best_key:
                best, best_key = obj, key
        tbl = _df_like_to_chart_table(best)
        if tbl:
            return tbl

        # (b) Fallback: artist extraction from the (single) plotted axis.
        for ax in (plotted_axes or fig.get_axes()):
            t = _mpl_axis_artist_table(ax)
            if t:
                return {"columns": t["columns"], "rows": t["rows"], "total_rows": t["total_rows"]}
        # (c) Nothing tabular (wordcloud / venn / network) → no button.
        return None
    except Exception:
        return None


def _format_plotly_numeric(fig):
    """Apply thousands grouping to NUMERIC Plotly axes + numeric data labels.

    Display polish only — never raises. Date and categorical axes are left
    untouched; year/id-style axes (by title) are skipped.
    """
    try:
        import numbers
        traces = list(getattr(fig, "data", None) or [])

        def _is_num(v):
            return isinstance(v, numbers.Number) and not isinstance(v, bool)

        def _axis_numeric(letter):
            vals = []
            for tr in traces:
                arr = getattr(tr, letter, None)
                if arr is None:
                    continue
                for v in list(arr)[:50]:
                    if v is not None:
                        vals.append(v)
                if len(vals) >= 50:
                    break
            return bool(vals) and all(_is_num(v) for v in vals)

        layout = getattr(fig, "layout", None)
        for letter, axis_name in (("x", "xaxis"), ("y", "yaxis")):
            try:
                axis = getattr(layout, axis_name, None)
                atype = getattr(axis, "type", None) if axis is not None else None
                if atype in ("date", "category", "multicategory", "log"):
                    continue
                # skip only genuine year/id-style axes (head-word match)
                t = getattr(axis, "title", None) if axis is not None else None
                ttxt = (getattr(t, "text", None) or "") if t is not None else ""
                if _is_identifier_label(ttxt):
                    continue
                if atype == "linear" or (atype in (None, "-") and _axis_numeric(letter)):
                    fig.update_layout(**{axis_name: dict(tickformat=",")})
            except Exception:
                continue

        # ── Numeric value labels → thousands-grouped ──────────────────────
        # A trace can show numeric value labels three ways, all handled here:
        #   (A) an explicit numeric `trace.text` array (incl. text bound to a
        #       numeric column — px passes it through as `text`),
        #   (B) `text_auto=True` / any preset `texttemplate` that binds a BARE
        #       numeric variable (Plotly emits e.g. "%{y}" with text=None),
        # In both cases we (re)bind/regroup to the trace's NUMERIC value so the
        # d3 "," group actually applies (Plotly re-stringifies `text`, so
        # "%{text:,}" would not group). Genuine string labels, already-formatted
        # templates ("%{y:.2s}"), and year/id series (1900–2100) are left alone.
        def _as_num(v):
            if v is None:
                return None
            if _is_num(v):
                return v
            if isinstance(v, str):
                s = v.replace(",", "").strip()
                if s == "":
                    return None
                f = float(s)              # ValueError on 'North', '60k', … → skip trace
                return int(f) if f.is_integer() else f
            raise ValueError("non-numeric label")

        def _value_var(tr):
            ttype = getattr(tr, "type", None)
            if ttype in ("pie", "funnelarea"):
                return "value"
            if ttype == "bar" and getattr(tr, "orientation", None) == "h":
                return "x"
            return "y"

        def _bound_numeric(tr, var):
            """Bound numeric values for `var`, or None if empty / has a real
            (non-numeric) string entry."""
            arr = getattr(tr, "values" if var == "value" else var, None)
            if arr is None:
                return None
            nums = []
            for v in arr:
                if v is None:
                    continue
                if _is_num(v):
                    nums.append(float(v))
                elif isinstance(v, str):
                    try:
                        nums.append(float(v.replace(",", "").strip()))
                    except Exception:
                        return None
                else:
                    return None
            return nums or None

        def _is_year_series(nums):
            return all(float(n).is_integer() and 1900 <= int(n) <= 2100 for n in nums)

        def _grouped_fmt(nums):
            """d3 number format for value labels, decided from the VALUES (not any
            column name): thousands-grouped, with 0 decimals for an integer-valued
            series and at most 2 (trailing zeros trimmed) otherwise. Keeps a mean
            like 12345.6789 as "12,345.68" instead of the raw "%{y:,}" full
            precision, while integer counts stay clean ("12,345")."""
            try:
                allint = all(float(n).is_integer() for n in nums)
            except Exception:
                allint = False
            return ",.0f" if allint else ",.2~f"

        for tr in traces:
            try:
                var = _value_var(tr)
                # (A) explicit numeric trace.text
                txt = getattr(tr, "text", None)
                if txt is not None and not isinstance(txt, str):
                    seq = list(txt) if hasattr(txt, "__iter__") else []
                    if seq:
                        parsed = [_as_num(v) for v in seq]   # raises on a real string → except
                        nums = [p for p in parsed if p is not None]
                        if nums and not all(isinstance(p, int) and 1900 <= p <= 2100 for p in nums):
                            tr.texttemplate = "%{" + var + ":" + _grouped_fmt(nums) + "}"
                    continue
                # (B) text_auto / preset texttemplate binding a BARE numeric var
                tt = getattr(tr, "texttemplate", None)
                bare = "%{" + var + "}"
                if isinstance(tt, str) and bare in tt:
                    nums = _bound_numeric(tr, var)
                    if nums and not _is_year_series(nums):
                        tr.texttemplate = tt.replace(bare, "%{" + var + ":" + _grouped_fmt(nums) + "}")
            except Exception:
                continue
    except Exception:
        pass


# Conditional log scaling for value axes with an extreme positive dynamic range.
_LOG_RANGE_RATIO = 1000.0   # max/median (or p95/p5) must exceed this to switch
_LOG_MIN_POINTS = 4         # need enough numeric points to judge a range
# Only simple bar/line/scatter charts are eligible; any other trace type aborts.
_LOG_ALLOWED_TYPES = {"bar", "scatter", "scattergl"}


def _maybe_log_plotly_axes(fig):
    """Switch a NUMERIC value axis to a log scale when its plotted values span
    many orders of magnitude, so a few extreme values don't compress the rest
    into an unreadable sliver. Generic + statistical: decided from the values
    themselves (max/median and p95/p5 ratios), never column names. Applies ONLY
    when ALL hold: every value on that axis is strictly > 0, the range ratio
    exceeds _LOG_RANGE_RATIO, the axis is linear (not date/category/already-log),
    and the figure is a plain bar/line/scatter. Otherwise the axis is left
    exactly as-is. Never raises (Article IV) — on any issue the chart is
    unchanged (linear)."""
    try:
        import numbers as _numbers
        traces = list(getattr(fig, "data", None) or [])
        if not traces:
            return
        # Abort on any non-simple trace (pie/treemap/heatmap/box/histogram/…):
        # log scaling is only meaningful and safe for bar/line/scatter values.
        for tr in traces:
            if (getattr(tr, "type", None) or "scatter") not in _LOG_ALLOWED_TYPES:
                return

        layout = getattr(fig, "layout", None)

        def _nums_on(letter):
            out = []
            for tr in traces:
                arr = getattr(tr, letter, None)
                if arr is None:
                    continue
                for v in arr:
                    if isinstance(v, _numbers.Number) and not isinstance(v, bool):
                        out.append(float(v))
            return out

        def _pct(svals, p):
            if not svals:
                return None
            k = (len(svals) - 1) * p
            f = int(k)
            if f + 1 >= len(svals):
                return svals[-1]
            return svals[f] + (svals[f + 1] - svals[f]) * (k - f)

        for letter, axis_name in (("x", "xaxis"), ("y", "yaxis")):
            try:
                vals = _nums_on(letter)
                if len(vals) < _LOG_MIN_POINTS:
                    continue
                # (a) strictly positive — log is undefined for <= 0
                if any(v <= 0 for v in vals):
                    continue
                axis = getattr(layout, axis_name, None)
                atype = getattr(axis, "type", None) if axis is not None else None
                # (c) only an ordinary linear numeric axis (skip date/category/log)
                if atype in ("date", "category", "multicategory", "log"):
                    continue
                # (b) genuinely huge dynamic range by a statistical ratio
                svals = sorted(vals)
                med = svals[len(svals) // 2] if len(svals) % 2 else \
                    0.5 * (svals[len(svals) // 2 - 1] + svals[len(svals) // 2])
                p5, p95 = _pct(svals, 0.05), _pct(svals, 0.95)
                ratio_med = (svals[-1] / med) if med > 0 else float("inf")
                ratio_pct = (p95 / p5) if (p5 and p5 > 0) else float("inf")
                if ratio_med >= _LOG_RANGE_RATIO or ratio_pct >= _LOG_RANGE_RATIO:
                    # merge (don't overwrite) so tickformat etc. set earlier stays
                    fig.update_layout(**{axis_name: dict(type="log")})
            except Exception:
                continue
    except Exception:
        pass


def _format_matplotlib_numeric(fig):
    """Apply thousands grouping to NUMERIC matplotlib axis ticks + numeric data
    labels. Display polish only — never raises (Article IV). Date/category axes
    and year/id-style axes (by axis label) are left alone."""
    try:
        import matplotlib.ticker as _mtick
        from matplotlib import dates as _mdates
        try:
            from matplotlib import category as _mcat
            _StrCat = (_mcat.StrCategoryFormatter,)
        except Exception:
            _StrCat = tuple()

        def _fmt(x, pos=None):
            try:
                return f"{int(x):,}" if float(x).is_integer() else f"{x:,.2f}"
            except Exception:
                return str(x)

        def _skip_axis(axis):
            try:
                fmt = axis.get_major_formatter()
                if isinstance(fmt, (_mdates.DateFormatter, _mdates.AutoDateFormatter,
                                    _mdates.ConciseDateFormatter)):
                    return True
                if _StrCat and isinstance(fmt, _StrCat):
                    return True
                conv = getattr(axis, "converter", None)
                if conv is not None and isinstance(conv, _mdates.DateConverter):
                    return True
            except Exception:
                pass
            return False

        def _parse_num(s):
            try:
                s = str(s).replace("−", "-").replace(",", "").strip()
                if s == "":
                    return None
                return float(s)
            except Exception:
                return None

        def _ticklabels_all_numeric(axis):
            """True ONLY if the axis currently shows tick labels and EVERY
            non-empty one parses as a number. A category axis, or a numeric axis
            relabeled with set_xticklabels(names) (the range()+set_xticklabels
            anti-pattern), has string labels → False → we must NOT overwrite them
            with 0,1,2…. Empty/undrawn → False (skip, never mislabel)."""
            try:
                texts = [t.get_text() for t in axis.get_ticklabels()]
            except Exception:
                return False
            texts = [s for s in texts if s and s.strip()]
            if not texts:
                return False
            return all(_parse_num(s) is not None for s in texts)

        # Tick labels are only populated after a draw — render once so the
        # numeric-vs-string check below sees the real labels (Agg, cheap).
        try:
            fig.canvas.draw()
        except Exception:
            pass

        for ax in fig.get_axes():
            for axis, label in ((ax.xaxis, ax.get_xlabel()), (ax.yaxis, ax.get_ylabel())):
                try:
                    if _skip_axis(axis):
                        continue
                    if _is_identifier_label(label):
                        continue
                    # Only group an axis whose CURRENT tick labels are all
                    # numeric. Skipping a string/category axis preserves names
                    # like brand/category labels (never replaced by 0,1,2…).
                    if not _ticklabels_all_numeric(axis):
                        continue
                    axis.set_major_formatter(_mtick.FuncFormatter(_fmt))
                except Exception:
                    continue
            # Numeric data labels (bar labels / annotations). Plain numbers only;
            # a 4-digit year is left alone, 'k'/'M'-suffixed or text labels skip.
            for t in list(getattr(ax, "texts", []) or []):
                try:
                    s = t.get_text()
                    if not s:
                        continue
                    val = float(s.replace(",", "").strip())
                    if float(val).is_integer():
                        iv = int(val)
                        if 1900 <= iv <= 2100:
                            continue
                        t.set_text(f"{iv:,}")
                    else:
                        t.set_text(f"{val:,.2f}")
                except Exception:
                    continue
    except Exception:
        pass


def render_plot_safe(code: str, dfs: dict, sid_or_id: str, split_multi_axes: bool = False):
    """Execute plotting code in a limited scope and return {ok, image|error}.

    The environment exposes pd/np/plt/mpatches/sns/go/px and the loaded dataframes as `dfs`.
    Supports Matplotlib, Seaborn, and Plotly visualizations.

    One-figure-per-chart guard: if the matplotlib figure has >1 real plotting
    axes laid out in a single row/column (a plt.subplots(1,N)/(N,1) mistake), by
    default this returns a `multi_axes` error so the caller can regenerate the
    chart as SEPARATE ###NEXT_PLOT### blocks. With `split_multi_axes=True` it
    instead splits the figure into one cropped image (+ its own data) per axis
    and returns them under `multi_charts` — so the user never sees two plots in
    one image.
    """
    import logging
    import re as _re
    logging.info(f"[PLOT_RENDER] Starting plot execution, go={go is not None}, px={px is not None}")
    env = dict(GLOBAL_PLOT_SCOPE)
    env["dfs"] = dfs
    # Same guarded builtins as code_exec.safe_execute — chart code must not be
    # able to import DB drivers / credential modules either (Article VII).
    # dict(): per-call copy so one execution can't mutate the shared guard.
    from sandbox_guard import SANDBOX_BUILTINS
    env["__builtins__"] = dict(SANDBOX_BUILTINS)

    # Strip direct upsetplot imports — the library's UpSet class has rendering
    # bugs (NaN RGBA errors). Use upset_plot_from_sets() from scope instead.
    code = _re.sub(
        r'^[ \t]*(from\s+upsetplot\s+import\s+.+)$',
        r'# \1  # blocked: use upset_plot_from_sets() instead',
        code,
        flags=_re.MULTILINE,
    )

    # Neutralize browser-opening show() calls before exec. fig.show() /
    # pio.show() / plotly.offline.plot()/iplot() each spawn a temporary HTTP
    # server on a random port and pop a browser tab when exec'd. The Plotly
    # renderer is also pinned to "json" at module load — this strip is belt-and-
    # suspenders (and stops the json renderer from dumping the figure to stdout).
    # The chart is always captured afterwards by _plotly_to_html(); show() is
    # never needed for capture, so this changes nothing visible.
    code, _n_show = _re.subn(
        r'^([ \t]*)((?:[A-Za-z_][\w.]*\.show|pio\.show|plotly\.offline\.i?plot|iplot)\s*\([^\n]*\)[ \t]*)$',
        r'\1pass  # \2 -- blocked: browser-opening show() disabled',
        code,
        flags=_re.MULTILINE,
    )
    if _n_show:
        logging.info(f"[PLOT_RENDER] PLOT_SHOW_STRIPPED count={_n_show} sid={sid_or_id}")
    # Belt-and-suspenders: a bare show(...) call resolves to this harmless no-op.
    env["show"] = lambda *a, **k: None

    try:
        exec(code, env, env)
        logging.info(f"[PLOT_RENDER] Code executed, checking for figures...")
        
        # Check if a Plotly figure was created
        plotly_fig = None
        for var_name in ['fig', 'figure', 'plot']:
            if var_name in env:
                obj = env[var_name]
                obj_type = str(type(obj))
                logging.info(f"[PLOT_RENDER] Found '{var_name}': {obj_type}")
                
                # Check if it's a Plotly figure (works for both go.Figure and px figures)
                has_to_html = hasattr(obj, 'to_html')
                has_data = hasattr(obj, 'data')
                logging.info(f"[PLOT_RENDER] go={go is not None}, has_to_html={has_to_html}, has_data={has_data}")
                
                if has_to_html and has_data:
                    # It's a Plotly figure (don't need to check 'go' since we check attributes)
                    logging.info(f"[PLOT_RENDER] Detected Plotly figure: {var_name}")
                    plotly_fig = obj
                    break
        
        if plotly_fig is not None:
            # One-chart-per-image guard for Plotly multi-CELL subplot GRIDS
            # (make_subplots rows>1/cols>1 or facets). A secondary_y dual-axis is
            # ONE cell (shared x, overlaying y2) and is explicitly allowed.
            if _is_plotly_grid(plotly_fig):
                if not split_multi_axes:
                    logging.info(f"[PLOT_RENDER] PLOTLY_GRID_DETECTED sid={sid_or_id}")
                    return {
                        "ok": False, "multi_axes": True, "is_plotly": True,
                        "error": (
                            "MultiAxesChartError: your code combined multiple charts in ONE figure "
                            "via plotly make_subplots with more than one cell (rows>1 or cols>1) or "
                            "facets. Charts about DIFFERENT entities or with DIFFERENT x-axes MUST be "
                            "SEPARATE single-figure plot_code blocks split by ###NEXT_PLOT### — one "
                            "go.Figure / px chart each. make_subplots is allowed ONLY in its single-cell "
                            "dual-axis form make_subplots(specs=[[{\"secondary_y\": True}]]) where both "
                            "metrics share the SAME x-axis."
                        ),
                        "trace": "",
                    }
                charts = [c for c in _split_plotly_grid(plotly_fig) if c.get("image")]
                if charts:
                    logging.info(f"[PLOT_RENDER] PLOTLY_GRID_SPLIT charts={len(charts)} sid={sid_or_id}")
                    return {"ok": True, "is_plotly": True, "multi_charts": charts}
                # Could not split — fall through and render the grid as-is (better
                # one combined chart than nothing).
            # Handle Plotly figure - return interactive HTML for 3D rotation support
            logging.info("[PLOT_RENDER] Rendering Plotly as interactive HTML")
            html = _plotly_to_html(plotly_fig)
            logging.info(f"[PLOT_RENDER] Generated HTML: {len(html)} chars, starts with: {html[:100]}")
            # Capture the plotted data for the client-side "Show data" button.
            # Display-only; failures degrade to None and never block the chart.
            chart_data = _extract_plotly_chart_data(plotly_fig)
            return {"ok": True, "plotly_html": html, "is_plotly": True, "chart_data": chart_data}
        else:
            # Empty-data guard: check if any axes have actual data before encoding
            fig = plt.gcf()
            axes = fig.get_axes()
            if axes and not any(ax.has_data() for ax in axes):
                plt.close(fig)
                return {"ok": False, "error": "EmptyChartError: Chart rendered with no data. The filtered dataset may be empty or the column values may not match the expected filter criteria.", "trace": ""}
            # Near-zero value guard for bar charts: detect meaningless near-zero data
            # Aggregate ALL bars across ALL containers (seaborn may create 1 container per bar)
            # Only trigger when ALL bars are near zero — a single low-value bar is legitimate data
            for ax in axes:
                if not ax.containers:
                    continue
                all_bar_vals = []
                for container in ax.containers:
                    for p in container:
                        if hasattr(p, 'get_height'):
                            all_bar_vals.append(_bar_data_value(p))
                if len(all_bar_vals) >= 3:
                    max_abs = max(abs(v) for v in all_bar_vals)
                    data_range = max(all_bar_vals) - min(all_bar_vals)
                    if max_abs < 0.1 and data_range < 0.1:
                        plt.close(fig)
                        return {
                            "ok": False,
                            "error": (
                                f"NearZeroChartError: All bar values are near zero (max={max_abs:.4f}, range={data_range:.4f}). "
                                "This likely indicates a data type error — numeric operations were applied to non-numeric or boolean data. "
                                "Check column dtypes and recompute the metric correctly."
                            ),
                            "trace": "",
                        }
            # Mostly-zero bar guard: detect charts where most bars are zero/NaN
            # This catches the categorical mapping bug: .map() misses most values → NaN → 0-height bars
            for ax in axes:
                if not ax.containers:
                    continue
                for container in ax.containers:
                    bar_vals = [_bar_data_value(p) for p in container if hasattr(p, 'get_height')]
                    if len(bar_vals) >= 3:  # Only check charts with 3+ bars
                        zero_count = sum(1 for v in bar_vals if abs(v) < 1e-9)
                        nonzero_count = len(bar_vals) - zero_count
                        if zero_count > len(bar_vals) * 0.5 and nonzero_count >= 1:
                            plt.close(fig)
                            return {
                                "ok": False,
                                "error": (
                                    f"MostlyEmptyChartError: {zero_count} out of {len(bar_vals)} bars have zero/NaN values. "
                                    "This usually means .map() with an incomplete mapping dict missed most categorical string values "
                                    "(unmapped strings become NaN, which plot as zero). "
                                    "Fix: for EACH categorical column, first inspect ALL unique values with "
                                    "df[col].dropna().str.lower().str.strip().unique(), then build a mapping dict "
                                    "that covers EVERY value. Each column may have different value sets — "
                                    "do NOT reuse one mapping across columns with different categorical values."
                                ),
                                "trace": "",
                            }
            # Aggregate bar guard: seaborn barplot creates one container per category,
            # so the per-container check above (len >= 3) won't trigger.
            # This collects ALL bars across ALL containers on each axis.
            for ax in axes:
                if len(ax.containers) < 3:
                    continue
                all_bars = []
                for container in ax.containers:
                    for p in container:
                        if hasattr(p, 'get_height'):
                            all_bars.append(_bar_data_value(p))
                if len(all_bars) >= 3:
                    zero_count = sum(1 for v in all_bars if abs(v) < 1e-9)
                    nonzero_count = len(all_bars) - zero_count
                    if zero_count > len(all_bars) * 0.5 and nonzero_count >= 1:
                        plt.close(fig)
                        return {
                            "ok": False,
                            "error": (
                                f"MostlyEmptyChartError: {zero_count} out of {len(all_bars)} bars have zero/NaN values "
                                f"(across {len(ax.containers)} bar groups). "
                                "This usually means .map() with an incomplete mapping dict missed most categorical string values "
                                "(unmapped strings become NaN, which plot as zero). "
                                "Fix: for EACH categorical column, first inspect ALL unique values with "
                                "df[col].dropna().str.lower().str.strip().unique(), then build a mapping dict "
                                "that covers EVERY value. Each column may have different value sets — "
                                "do NOT reuse one mapping across columns with different categorical values."
                            ),
                            "trace": "",
                        }
            # --- Detect non-standard visualizations (Venn, wordcloud, treemap, network) ---
            # These use matplotlib primitives that the sparse/bar guards cannot count.
            # If detected, skip the sparse guard entirely.
            _is_nonstandard_viz = False
            for ax in axes:
                # Venn diagrams / treemaps: have multiple non-pie patches (no theta1)
                non_pie_patches = [p for p in ax.patches if not hasattr(p, 'theta1')]
                if len(non_pie_patches) >= 2:
                    _is_nonstandard_viz = True
                    break
                # Word clouds: rendered as AxesImage
                if ax.images:
                    _is_nonstandard_viz = True
                    break
                # Network graphs (networkx): collections with many paths but no bar containers
                if len(ax.collections) > 0 and not ax.containers:
                    for coll in ax.collections:
                        if hasattr(coll, 'get_paths') and len(coll.get_paths()) > 2:
                            _is_nonstandard_viz = True
                            break
                if _is_nonstandard_viz:
                    break

            # Sparse data guard: only for standard chart types (bar, scatter, line, pie)
            if not _is_nonstandard_viz:
                total_data_points = 0
                for ax in axes:
                    # Bar chart bars (non-zero height)
                    for container in ax.containers:
                        total_data_points += sum(
                            1 for p in container
                            if hasattr(p, 'get_height') and abs(_bar_data_value(p)) > 0
                        )
                    # Pie wedges (patches with theta1 attribute)
                    total_data_points += sum(1 for p in ax.patches if hasattr(p, 'theta1'))
                    # Scatter / collection points
                    for coll in ax.collections:
                        if hasattr(coll, 'get_offsets'):
                            offsets = coll.get_offsets()
                            if hasattr(offsets, '__len__'):
                                total_data_points += len(offsets)
                    # Line data (exclude grid/axis lines which typically have <= 2 points)
                    if not ax.containers:
                        for line in ax.lines:
                            xdata = line.get_xdata()
                            if hasattr(xdata, '__len__') and len(xdata) > 2:
                                total_data_points += len(xdata)
                if total_data_points < 2 and total_data_points >= 0:
                    plt.close(fig)
                    return {
                        "ok": False,
                        "error": (
                            f"SparseChartError: Chart contains only {total_data_points} data point(s), which is too few to be meaningful. "
                            "This usually means .map() with a hardcoded dictionary missed most actual values (they became NaN), "
                            "or .dropna() removed rows with unmapped values. "
                            "Fix: inspect df['col'].unique() first, then build a mapping covering ALL unique values."
                        ),
                        "trace": "",
                    }
            # --- One-figure-per-chart HARD GUARD (client-side enforcement) ---
            # The brain prompt forbids combining plots, but the LLM still emits
            # plt.subplots(1,2) etc. If >1 real STANDARD plotting axes (bars/lines
            # — which excludes venn/wordcloud/network/treemap that have no
            # containers/lines) sit in a single row/column, either signal the
            # caller to regenerate as separate ###NEXT_PLOT### blocks (default) or
            # split the figure into one image per axis (fallback).
            if True:
                real_axes = [a for a in _real_plot_axes(fig) if _mpl_axis_has_plot(a)]
                if len(real_axes) > 1 and _axes_single_row_or_col(real_axes):
                    if not split_multi_axes:
                        plt.close(fig)
                        logging.info(f"[PLOT_RENDER] MULTI_AXES_DETECTED axes={len(real_axes)} sid={sid_or_id}")
                        return {
                            "ok": False,
                            "multi_axes": True,
                            "error": (
                                "MultiAxesChartError: your code combined multiple plots in ONE figure "
                                "via plt.subplots()/add_subplot with more than one axes. Regenerate as "
                                "SEPARATE single-figure plot_code blocks, ONE figure each, split by "
                                "###NEXT_PLOT###. Do NOT use plt.subplots() with more than one axes."
                            ),
                            "trace": "",
                        }
                    # Fallback: one cropped image + its own data table per axis.
                    imgs = _split_figure_by_axis(fig, real_axes)
                    charts = []
                    for a, im in zip(real_axes, imgs):
                        if not im:
                            continue
                        charts.append({
                            "image": im,
                            "title": (a.get_title() or "").strip() or None,
                            "chart_data": _mpl_axis_artist_table(a),
                        })
                    plt.close(fig)
                    logging.info(f"[PLOT_RENDER] MULTI_AXES_SPLIT charts={len(charts)} sid={sid_or_id}")
                    return {"ok": True, "is_plotly": False, "multi_charts": charts}

            # Handle Matplotlib/Seaborn figure
            logging.info(f"[PLOT_RENDER] No Plotly figure found, trying matplotlib axes={len(axes)}, nonstandard_viz={_is_nonstandard_viz}")
            # Capture the plotted data for the client-side "Show data" button
            # BEFORE _encode_current_figure() closes the figure. Display-only;
            # failures degrade to None (no button) and never block the chart.
            chart_data = _extract_matplotlib_chart_data(fig, env, dfs)
            img = _encode_current_figure()
            logging.info(f"[PLOT_RENDER] Generated matplotlib image: {len(img)} chars")
            return {"ok": True, "image": img, "is_plotly": False, "chart_data": chart_data}
            
    except Exception as e:
        tb = traceback.format_exc(limit=3)
        logging.error(f"Plot rendering error: {e}\n{tb}")
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "trace": tb}
