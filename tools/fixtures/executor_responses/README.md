# Captured executor responses

Real `POST /execute` responses from the analysis-sandbox image, stored as the
wire contract between the two images. `tests/test_executor_dispatch.py` replays
them through an `httpx.MockTransport` so the dispatcher, the reconstruction and
the whole answer path can be tested on any platform — the sandbox service
itself needs Linux (`pass_fds`, `start_new_session`, `/proc`).

They exist because the two images are versioned and upgraded independently. A
customer can run a new web image against an older sandbox image, so the
response grammar is a stored-shape contract: a renamed or dropped field must
fail a test here rather than surface later as "the answer lost its chart".

## Layout

```
<case>/case.json       the code, kind, timeout, http status, resulting payload keys
<case>/request.json    the body that was posted (job_id replaced by <job_id>)
<case>/response.json   the response body, VERBATIM bytes
<case>/out/            every file the sandbox wrote into the job directory
```

The test copies `out/` into the job directory named in the request before
returning the body, because `result` entries reference `out/<file>` relative to
it.

## What each case pins

| Case | The property it exists for |
|---|---|
| `python_frame` | a DataFrame result crosses as one uncompressed parquet file |
| `python_scalar` | a scalar is inline, no file |
| `python_dict_styler` | a dict result writes one parquet per entry, and only the Styler entry carries `styled_html` |
| `python_nan_preview` | NaN is the literal `NaN` token in both `result` and `preview`, never `null` |
| `python_error` | the error shape carries `error` ALONE |
| `python_timeout` | `status: timeout`, and an integral timeout renders `3 seconds`, not `3.0` |
| `plot_plotly` | the chart HTML references the locally served bundle and never `cdn.plot.ly` |
| `plot_matplotlib` | a base64 PNG travels inline |
| `plot_multi_axes_error` | the matplotlib refusal has NO `is_plotly` key — the ABSENCE is the contract, since callers tell the two producers apart by which keys exist |
| `plot_multi_charts` | `split_multi_axes` yields `multi_charts` with one entry per axis |

## Re-capturing

Needs a built sandbox image and Docker. The point of the procedure is that the
bodies are produced by the real service, over real HTTP, through the real
two-identity job directory — not hand-written.

```sh
# 1. seed a throwaway jobs volume FROM THE SANDBOX IMAGE (it ships /jobs as
#    root:<shared gid> 2770; a volume first written under the web image comes
#    up root:root 755 and neither identity can create a job directory)
docker run --rm -v sandbox_capture_jobs:/jobs powerdatachat-executor:enterprise true

# 2. run the service on the web stack's network, with the runtime hardening
docker run -d --name pdc-sandbox-capture --network pdc_client_default \
  -v sandbox_capture_jobs:/jobs --read-only --tmpfs /tmp:size=1g,mode=1777 \
  --cap-drop ALL --security-opt no-new-privileges:true \
  --memory 3g --pids-limit 256 powerdatachat-executor:enterprise

# 3. drive it AS THE WEB IDENTITY (uid 10001) from the web image, so the job
#    directory is created and the inputs written exactly as in production
docker run --rm --network pdc_client_default --user 10001:10001 \
  -v sandbox_capture_jobs:/jobs -v "$PWD/tools/capture_executor_fixtures.py:/capture.py:ro" \
  -e EXEC_URL=http://pdc-sandbox-capture:8090 \
  powerdatachat-client:enterprise python /capture.py

# 4. copy the captures out, then remove the container and the volume
docker run --rm -v sandbox_capture_jobs:/jobs -v "$PWD/tools/fixtures:/dest" alpine \
  sh -c "rm -rf /dest/executor_responses/* && cp -a /jobs/_captures/. /dest/executor_responses/"
docker rm -f pdc-sandbox-capture
```

`out/` holds parquet written by the pinned pyarrow version. Do not re-save
these files with any other tool: they are also the evidence that the reader's
uncompressed-only rule accepts what the writer actually produces.
