# syntax=docker/dockerfile:1
#
# **CI only. Never built, tagged or pushed by release.yml** -- and
# tests/test_self_update_ci.py fails if release.yml ever names this file, this
# image or anything under tests/.
#
# The end-to-end self-update job (design notes 15.5) runs the real updater
# image in its container, as compose runs it, with one thing replaced:
# verification. Nothing the job builds has an attestation, so this image is
# the release's real updater image (`--target updater` of the Dockerfile, the
# exact image a release ships) with the test-only trust policy added on top
# and selected by the entry point. The published updater carries no `tests/`
# at all, and `python -m updater` builds only `trust.Sigstore`.
#
#     docker build -f tests/self_update/ci-updater.Dockerfile \
#         --build-arg UPDATER_IMAGE=<the release's updater image> \
#         -t spend-tracker-ci-updater:<version> .
#
# Its local name, `spend-tracker-ci-updater`, is in no repository release.yml
# pushes to. The job pushes it, under the updater's repository name, only to
# the registry that answers as ghcr.io on its own runner.
ARG UPDATER_IMAGE
FROM ${UPDATER_IMAGE}
COPY tests/__init__.py ./tests/__init__.py
COPY tests/self_update/__init__.py tests/self_update/ci_trust.py tests/self_update/ci_updater.py ./tests/self_update/
ENTRYPOINT ["python", "-m", "tests.self_update.ci_updater"]
