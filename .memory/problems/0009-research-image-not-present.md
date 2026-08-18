# 0009 — 本地缺少多 Trajectory 研究 Docker 镜像

> Purpose: record the reproducible prerequisite blocking real-environment
> research Harness acceptance and all real research Agent runs.

Status: Open
Observed: 2026-08-14

## Reproduction

From the repository root, inspect the required local image with Docker socket
access:

```bash
docker image inspect python:3.11-slim
```

The Docker daemon responds, but the default immutable research image reference
`python:3.11-slim` is not present locally. This was reconfirmed on 2026-08-14
after the complete regression run. The framework reports the missing image and
does not pull it.

## Impact

- Unit and fake-integration tests can validate fail-closed behavior and the
  deterministic driver contract.
- A real Docker acceptance report cannot pass, so the smoke batch must remain
  `prepared` and no real Agent may run.
- This does not justify selecting an unrelated local image or falling back to
  host execution. A different image would change the certified capability
  identity and requires an explicit reviewed choice.

## Resolution condition

The project owner selects and provisions the intended research image through an
explicit external action. Then rerun `validate-harness`; the report must bind
the image's immutable SHA-256 ID and prove the active container configuration.
The framework must continue using `--pull never`.
