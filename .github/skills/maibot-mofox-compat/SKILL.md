---
name: maibot-mofox-compat
description: 'Synchronize MaiBot Plugin SDK versions, scan public capabilities, load trusted MaiBot plugins in Neo-MoFox, validate lifecycle and host mappings, and produce a compatibility gap report. Use for MaiBot SDK upgrades, plugin loading failures, capability coverage, or adapter maintenance.'
argument-hint: '[SDK tag or plugin path]'
user-invocable: true
disable-model-invocation: false
---

# MaiBot MoFox Compatibility

## Scope

Maintain `MofoxForMaibotPulgins` as a trusted, in-process compatibility bridge. The bridge must never inject a fake `maibot_sdk`, silently install third-party dependencies, or report success for a host capability that MoFox cannot execute.

## Procedure

1. Read the locked version and upstream snapshot in [UPSTREAM-COMPATIBILITY.md](../../docs/UPSTREAM-COMPATIBILITY.md).
2. Inspect the upstream `maibot_sdk` public exports and capability modules for the requested tag. Compare them with `sdk/__init__.py`, `runtime.py`, and `legacy_runtime.py`.
3. Classify each surface as `E` real execution, `D` data compatibility, `I` import compatibility, or `R` admission rejection. Update the matrix before changing behavior.
4. Keep plugin discovery deterministic: a single `plugin_path` wins; otherwise scan `plugins_root` for directories containing `plugin.py`. Prepare imports, config, components, and tools before the host component snapshot.
5. Test lifecycle transitions: `DISCOVERED -> PREPARED -> REGISTERED -> ACTIVATING -> ACTIVE`, quarantine failed activation, drain calls and tasks on unload, and remove private module prefixes.
6. Test source-bound message routing, structured send results, API identity/visibility, dynamic API replacement, LLM mapping, and explicit errors for unmapped capabilities.
7. Validate the bundled `wife_plugin` and focused tests under `tests/`.
8. Update [README.md](../../README.md), [DESIGN.md](../../docs/DESIGN.md), and the upstream snapshot whenever the support boundary changes.
9. Run the smallest relevant pytest selection and report any missing official SDK or host integration dependency as an environment limitation, not as a fake pass.

## Rules

- Only load trusted external code; this bridge is not a sandbox.
- Do not modify Neo-MoFox core from this adapter skill.
- Do not broaden `send.forward`, `send.hybrid`, `send.command`, `send.custom`, database, gateway, renderer, workflow, or provider behavior without a real host mapping and tests.
- Keep version checks exact and update the manifest dependency together with the tests and snapshot.
