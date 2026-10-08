"""The end-to-end self-update job's driver (design notes 15.5). Not collected by pytest.

Everything here is test-only and lives under `tests/` so the published
updater can never import it: the `self-update` job in
`.github/workflows/tests.yml` greps `updater/` for any reference to it and
fails if it finds one.
"""
