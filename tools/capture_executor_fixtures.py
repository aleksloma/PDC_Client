"""Capture REAL executor responses as test fixtures.

Runs INSIDE a client-image container as uid 10001 (the web identity), against
a standalone executor container on the same jobs volume — i.e. the real two-uid
path over real HTTP. For each case it stores, under /jobs/_captures/<case>/:

    request.json    the body that was posted (job_id replaced by a placeholder)
    response.json   the executor's response body, VERBATIM bytes
    out/...         every file the executor wrote into the job dir
    case.json       the code, kind, timeout and a note

The fixtures are the old-shape wire contract between the two images: a later
field rename shows up as a failing test instead of "the answer lost its chart".
"""
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, "/app")

import pandas as pd
import httpx

import exec_transport

JOBS = Path("/jobs")
OUT = JOBS / "_captures"
URL = os.environ.get("EXEC_URL", "http://pdc-sandbox-capture:8090")

FRAME = pd.DataFrame({
    "city": ["Tbilisi", "Batumi", "Tbilisi", "Kutaisi"],
    "product": ["A", "B", "A", "C"],
    "revenue": [100.5, 200.0, 50.25, 0.0],
})

CASES = [
    ("python_frame", "PYTHON", 60,
     "RESULT = dfs['sales'].groupby('city', as_index=False)['revenue'].sum()",
     "a DataFrame RESULT -> out/result.parquet"),
    ("python_scalar", "PYTHON", 60,
     "RESULT = float(dfs['sales']['revenue'].sum())",
     "a scalar RESULT, inline"),
    ("python_dict_styler", "PYTHON", 60,
     "d = dfs['sales']\n"
     "g = d.groupby('city', as_index=False)['revenue'].sum()\n"
     "RESULT = {'by city': g, 'styled': g.style.background_gradient(subset=['revenue']).format({'revenue': '{:.2f}'})}",
     "a dict RESULT holding a frame and a Styler -> one parquet each + styled_html"),
    ("python_nan_preview", "PYTHON", 60,
     "import numpy as np\nRESULT = float('nan')",
     "a NaN scalar: the preview must carry the literal NaN token"),
    ("python_error", "PYTHON", 60,
     "RESULT = dfs['sales']['nope'].sum()",
     "an ordinary execution error (KeyError)"),
    ("python_timeout", "PYTHON", 3,
     "while True:\n    pass",
     "the timeout path: status timeout, byte-identical error text"),
    ("plot_plotly", "PLOT", 120,
     "import plotly.express as px\n"
     "g = dfs['sales'].groupby('city', as_index=False)['revenue'].sum()\n"
     "fig = px.bar(g, x='city', y='revenue', title='revenue by city')",
     "a plotly figure: plotly_html must reference the LOCAL bundle, never cdn.plot.ly"),
    ("plot_matplotlib", "PLOT", 120,
     "import matplotlib.pyplot as plt\n"
     "g = dfs['sales'].groupby('city')['revenue'].sum()\n"
     "plt.figure()\nplt.bar(g.index, g.values)\nplt.title('revenue by city')",
     "a matplotlib figure: base64 PNG inline"),
    ("plot_multi_axes_error", "PLOT", 120,
     "import matplotlib.pyplot as plt\n"
     "fig, ax = plt.subplots(1, 2)\n"
     "ax[0].bar(['a','b'], [1,2])\nax[1].bar(['a','b'], [2,1])",
     "the matplotlib multi-axes refusal: NO is_plotly key (the absence is the contract)"),
    ("plot_multi_charts", "PLOT", 120,
     "import matplotlib.pyplot as plt\n"
     "fig, ax = plt.subplots(1, 2)\n"
     "ax[0].bar(['a','b'], [1,2])\nax[1].bar(['a','b'], [2,1])",
     "the same figure with split_multi_axes=True -> multi_charts of 2"),
]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"uid={os.getuid()} gid={os.getgid()} umask=0o{os.umask(0o022):03o} url={URL}")
    failures = []
    for name, kind, timeout_s, code, note in CASES:
        job_id = exec_transport.new_job_id()
        job_dir = exec_transport.create_job_dir(JOBS, job_id)
        manifest = exec_transport.write_inputs({"sales": FRAME}, job_dir, sid=job_id)
        body = {
            "job_id": job_id, "kind": kind, "code": code,
            "dataframes": manifest, "timeout_s": timeout_s,
            "options": {"split_multi_axes": name == "plot_multi_charts"},
        }
        try:
            with httpx.Client(timeout=httpx.Timeout(connect=5, read=timeout_s + 30,
                                                    write=30, pool=5)) as client:
                resp = client.post(f"{URL}/execute", content=exec_transport.dumps(body),
                                   headers={"content-type": "application/json"})
            raw = resp.content
            parsed = exec_transport.loads(raw)
            status = parsed.get("status")
            payload = parsed.get("payload") or {}
            dest = OUT / name
            shutil.rmtree(dest, ignore_errors=True)
            (dest / "out").mkdir(parents=True, exist_ok=True)
            # the response references out/<file> relative to the job dir
            src_out = job_dir / "out"
            copied = []
            if src_out.is_dir():
                for child in sorted(src_out.iterdir()):
                    if child.is_file():
                        shutil.copyfile(child, dest / "out" / child.name)
                        copied.append(f"{child.name} ({child.stat().st_size}B)")
            (dest / "response.json").write_bytes(raw)
            (dest / "request.json").write_text(
                exec_transport.dumps({**body, "job_id": "<job_id>"}), encoding="utf-8")
            (dest / "case.json").write_text(exec_transport.dumps({
                "case": name, "kind": kind, "timeout_s": timeout_s, "code": code,
                "note": note, "http_status": resp.status_code,
                "status": status, "payload_keys": sorted(payload.keys()),
                "out_files": copied, "body_bytes": len(raw),
            }), encoding="utf-8")
            print(f"  {name:24} http={resp.status_code} status={status:8} "
                  f"keys={sorted(payload.keys())} out={copied} bytes={len(raw)}")
        except Exception as e:
            failures.append(f"{name}: {type(e).__name__}: {e}")
            print(f"  {name:24} FAILED {type(e).__name__}: {e}")
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)
    print("\nFAILURES:" if failures else "\nall cases captured")
    for f in failures:
        print("  -", f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
