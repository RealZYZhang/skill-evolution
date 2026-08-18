# 0001 — Pi is installed but absent from the non-interactive PATH

Status: Mitigated
First observed: 2026-07-23

## Symptom

`npm list -g` reports `@earendil-works/pi-coding-agent@0.81.1`, while
`command -v pi` returns no result.

## Reproduction

Run both commands from the repository's non-interactive login shell.

## Diagnosis

The executable is linked inside the active NVM installation, but that
installation's `bin` directory is not present on the subprocess `PATH`.

## Workaround

The Python RPC client first checks an explicit command and `PATH`, then asks
`npm root -g` and resolves a known Pi package's `dist/cli.js`.

## Resolution

Mitigated in `scripts/pi_rpc.py` and covered by explicit-command unit tests.
Shell initialization may still be fixed separately for direct `pi` use.

