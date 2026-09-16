"""The `pdc-executor` service package.

`executor.app` is the HTTP surface (`GET /healthz`, `POST /execute`) and
`executor.runner` is the per-job subprocess entry point. Nothing here imports
the main app's stores, credentials or DB modules — the whole point of the
container is that generated Python runs where none of them exist.

Deliberately empty of logic: importing this package must not start a thread,
read the environment or touch the filesystem (the app lifespan owns all of
that), so `python -c "import executor"` stays free of side effects.
"""
