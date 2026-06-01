# plot_utils.py — v2.0
"""Helpers to execute plotting code (Matplotlib, Seaborn, Plotly) and capture figures as base64 images."""
import io, base64, traceback
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
except ImportError:
    go = None
    px = None
    pio = None

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
        
        # Generate standalone HTML with responsive sizing
        html = fig.to_html(
            include_plotlyjs='cdn',
            config={
                'displayModeBar': True,
                'displaylogo': False,
                'modeBarButtonsToRemove': ['toImage', 'sendDataToCloud'],
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

def render_plot_safe(code: str, dfs: dict, sid_or_id: str):
    """Execute plotting code in a limited scope and return {ok, image|error}.

    The environment exposes pd/np/plt/mpatches/sns/go/px and the loaded dataframes as `dfs`.
    Supports Matplotlib, Seaborn, and Plotly visualizations.
    """
    import logging
    import re as _re
    logging.info(f"[PLOT_RENDER] Starting plot execution, go={go is not None}, px={px is not None}")
    env = dict(GLOBAL_PLOT_SCOPE)
    env["dfs"] = dfs

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
            # Handle Plotly figure - return interactive HTML for 3D rotation support
            logging.info("[PLOT_RENDER] Rendering Plotly as interactive HTML")
            html = _plotly_to_html(plotly_fig)
            logging.info(f"[PLOT_RENDER] Generated HTML: {len(html)} chars, starts with: {html[:100]}")
            return {"ok": True, "plotly_html": html, "is_plotly": True}
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
            # Handle Matplotlib/Seaborn figure
            logging.info(f"[PLOT_RENDER] No Plotly figure found, trying matplotlib axes={len(axes)}, nonstandard_viz={_is_nonstandard_viz}")
            img = _encode_current_figure()
            logging.info(f"[PLOT_RENDER] Generated matplotlib image: {len(img)} chars")
            return {"ok": True, "image": img, "is_plotly": False}
            
    except Exception as e:
        tb = traceback.format_exc(limit=3)
        logging.error(f"Plot rendering error: {e}\n{tb}")
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "trace": tb}
