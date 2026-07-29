# code_exec.py — v2.4 (adds plotly and seaborn to execution environment)
import io, base64, traceback, hashlib, atexit
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import seaborn as sns
import plotly.express as px
import plotly.graph_objects as go
from logger_utils import log_with_sid
from settings import settings
from sandbox_guard import SANDBOX_BUILTINS
from exec_sanitizer import sanitize_for_execution

# --- Extended visualization libraries (safe imports) ---
try:
    from matplotlib_venn import venn2, venn3, venn2_circles, venn3_circles
except ImportError:
    venn2 = venn3 = venn2_circles = venn3_circles = None
try:
    from wordcloud import WordCloud
except ImportError:
    WordCloud = None
try:
    import networkx as nx
except ImportError:
    nx = None
try:
    import squarify
except ImportError:
    squarify = None
try:
    from scipy import stats as scipy_stats
except ImportError:
    scipy_stats = None
try:
    import missingno as msno
except ImportError:
    msno = None
try:
    import calplot
except ImportError:
    calplot = None
from plot_utils import upset_plot_from_sets
try:
    from adjustText import adjust_text
except ImportError:
    adjust_text = None

# --- Machine Learning libraries (safe imports) ---
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

# --- Georgian/Unicode font support (same as plot_utils.py) ---
try:
    from matplotlib import font_manager as _fm
    from pathlib import Path as _FontPath
    _font_candidates = [
        _FontPath(__file__).resolve().parent / "static" / "fonts" / "DejaVuSans.ttf",
        _FontPath("/app/static/fonts/DejaVuSans.ttf"),
    ]
    for _fp in _font_candidates:
        if _fp.exists():
            _fm.fontManager.addfont(str(_fp))
            matplotlib.rcParams['font.sans-serif'] = ['DejaVu Sans'] + matplotlib.rcParams.get('font.sans-serif', [])
            break
except Exception:
    pass

# Execution timeout in seconds (Windows-compatible using ThreadPoolExecutor)
CODE_EXEC_TIMEOUT_SECONDS = 60

# Module-level executor for code execution (reused across calls)
_EXEC_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="code_exec")


def _shutdown_exec_pool():
    """Shutdown the code execution thread pool."""
    _EXEC_POOL.shutdown(wait=False, cancel_futures=True)


atexit.register(_shutdown_exec_pool)

def _figure_to_base64_if_any():
    """Return base64 for current Matplotlib figure if it has drawn data; else None."""
    try:
        fig = plt.gcf()
        if fig is None:
            return None
        # skip if no axes or no data drawn
        if not fig.axes or all(not ax.has_data() for ax in fig.axes):
            plt.close(fig)
            return None
        buf = io.BytesIO()
        fig.tight_layout()
        fig.savefig(buf, format="png", dpi=settings.FIGURE_DPI, bbox_inches="tight", facecolor="white")
        plt.close(fig)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return None

def _preview_from_result(obj):
    """Build a small JSON-serializable preview from an execution result."""
    try:
        # Handle pandas Styler objects (extract underlying DataFrame)
        if hasattr(obj, 'data') and isinstance(obj.data, pd.DataFrame):
            return obj.data.head(settings.PREVIEW_HEAD_ROWS).to_dict(orient="records")
        if isinstance(obj, pd.DataFrame):
            return obj.head(settings.PREVIEW_HEAD_ROWS).to_dict(orient="records")
        if isinstance(obj, pd.Series):
            return obj.head(settings.PREVIEW_HEAD_ROWS).to_dict()
        if isinstance(obj, (list, dict, str, int, float, bool)) or obj is None:
            return obj
        return str(obj)[:settings.STR_PREVIEW_MAX_CHARS]
    except Exception:
        return None


def _execute_code_in_env(code: str, env: dict) -> dict:
    """Execute code in the given environment. Runs in a thread for timeout support.

    Returns dict with either 'error' key or 'result' from env['RESULT'].
    """
    try:
        # Strip direct upsetplot imports — the library's UpSet class has rendering
        # bugs (NaN RGBA errors). Use upset_plot_from_sets() from scope instead.
        import re as _re
        code = _re.sub(
            r'^[ \t]*(from\s+upsetplot\s+import\s+.+)$',
            r'# \1  # blocked: use upset_plot_from_sets() instead',
            code,
            flags=_re.MULTILINE,
        )
        exec(code, env, env)
        return {"success": True, "result": env.get("RESULT", None)}
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {e}"}


def safe_execute(code: str, dfs: dict, sid: str | None = None, timeout: float = None):
    """Execute trusted code with a constrained environment and extract outputs.

    Uses ThreadPoolExecutor for Windows-compatible timeout enforcement.
    If code exceeds the timeout, returns a timeout error without blocking.

    Args:
        code: Python code to execute
        dfs: Dictionary of DataFrames available to the code
        sid: Session ID for logging
        timeout: Execution timeout in seconds (default: CODE_EXEC_TIMEOUT_SECONDS)

    Returns a dict with possible keys: error, result, preview, image_base64.
    """
    if timeout is None:
        timeout = CODE_EXEC_TIMEOUT_SECONDS

    # Article XIII gate: generated code must only ever see standard dtypes.
    # Rebinding the local `dfs` covers both env["dfs"] and the `df` alias.
    dfs = sanitize_for_execution(dfs, sid or "exec")

    env = {
        "pd": pd, "np": np, "plt": plt,
        "sns": sns, "px": px, "go": go,  # seaborn and plotly
        # Extended visualization libraries
        "venn2": venn2, "venn3": venn3,
        "venn2_circles": venn2_circles, "venn3_circles": venn3_circles,
        "WordCloud": WordCloud, "nx": nx, "squarify": squarify,
        "scipy_stats": scipy_stats, "msno": msno, "calplot": calplot,
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
        "dfs": dfs, "RESULT": None,
        # Guarded builtins: without this key, exec() injects the FULL builtins
        # (incl. __import__) — generated code must never be able to import a
        # DB driver or this client's credential modules (Article VII; see
        # sandbox_guard.py for the honest limits of this guard).
        # PER-CALL COPY: generated code can assign into its __builtins__;
        # sharing one dict would let one execution poison the guard for every
        # later execution container-wide.
        "__builtins__": dict(SANDBOX_BUILTINS),
    }
    # Convenience alias: expose the first dataframe as `df` if available
    try:
        if isinstance(dfs, dict) and dfs:
            env.setdefault("df", next(iter(dfs.values())))
    except Exception:
        pass

    # Execute with timeout using module-level ThreadPoolExecutor
    try:
        future = _EXEC_POOL.submit(_execute_code_in_env, code, env)
        try:
            exec_result = future.result(timeout=timeout)
        except FuturesTimeoutError:
            # Code execution timed out
            try:
                h = hashlib.sha256(code.encode("utf-8", errors="ignore")).hexdigest()[:10]
                log_with_sid(sid or "exec", "error", f"EXEC_TIMEOUT after {timeout}s", code_hash=h)
            except Exception:
                pass
            return {"error": f"TimeoutError: Code execution exceeded {timeout} seconds limit"}

        if not exec_result.get("success"):
            # Execution error
            try:
                h = hashlib.sha256(code.encode("utf-8", errors="ignore")).hexdigest()[:10]
                snippet = code.strip().splitlines()[:settings.EXEC_ERROR_SNIPPET_LINES]
                log_with_sid(sid or "exec", "error", f"EXEC_ERROR {exec_result.get('error')}", code_hash=h, code=" \\n".join(snippet), dfs=list(dfs.keys()))
            except Exception:
                pass
            return {"error": exec_result.get("error")}

    except Exception as e:
        # Unexpected error in executor setup
        try:
            h = hashlib.sha256(code.encode("utf-8", errors="ignore")).hexdigest()[:10]
            snippet = code.strip().splitlines()[:settings.EXEC_ERROR_SNIPPET_LINES]
            log_with_sid(sid or "exec", "error", f"EXEC_ERROR {type(e).__name__}: {e}", code_hash=h, code=" \\n".join(snippet), dfs=list(dfs.keys()))
        except Exception:
            pass
        return {"error": f"{type(e).__name__}: {e}"}

    result = env.get("RESULT", None)
    preview = _preview_from_result(result)
    image_b64 = _figure_to_base64_if_any()
    try:
        meta = {
            "has_result": result is not None,
            "preview_type": type(preview).__name__ if preview is not None else "None",
            "image": bool(image_b64),
        }
        h = hashlib.sha256(code.encode("utf-8", errors="ignore")).hexdigest()[:10]
        log_with_sid(sid or "exec", "info", f"EXEC_OK code_hash={h}", **meta)
    except Exception:
        pass
    return {"error": None, "result": result, "preview": preview, "image_base64": image_b64}
