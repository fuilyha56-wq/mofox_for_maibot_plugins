# Upstream Compatibility Snapshot

## MaiBot Plugin SDK

- Repository: `https://github.com/Mai-with-u/maibot-plugin-sdk`
- Stable tag checked: `v2.17.0`
- Commit: `07821c0bcd14e9799cd219112a913040255c88f7`
- Checked: 2026-10-27
- Local source snapshot: `E:\plugins\tmp\maibot-sdk-v2.17.0.zip`

### Support levels

| Level | Meaning |
| --- | --- |
| E | Real execution through a Neo-MoFox public API |
| D | Importable/constructable data compatibility; execution is validated and may reject |
| I | Import compatibility only; execution raises a stable capability error |
| R | Admission rejection because the declaration cannot be safely represented |

| SDK surface | Level | Adapter behavior |
| --- | --- | --- |
| `Command`, `EventHandler(ON_MESSAGE)`, `Tool`, `Action`, `API` | E | Lifecycle-aware registration and dispatch |
| `send.text`, `send.image`, `send.emoji` | E | OneBot/host send path with structured result |
| `config.*`, `api.*`, `message.*`, `person.get_id`, `chat.open_session`, `llm.generate`, `component.get_all_plugins`, `component.get_plugin_info`, `component.list_loaded_plugins`, `component.list_registered_plugins`, `component.load_plugin`, `component.unload_plugin`, `component.reload_plugin` | E | Explicit host API mapping and source-bound routing; bridge self-management is rejected |
| `PluginContext`, `PluginPaths`, public component metadata | D | Official SDK objects are loaded and validated; unsupported fields fail at execution boundary |
| `database.*`, `frequency.*`, `knowledge.*`, `logging.*`, `maisaka.*`, `statistics.*`, `tool.*`, `files.*`, `market.*`, `web.*`, `internal_chat.*`, `desktop_pet.*` | I | Public names are recognized by the version matrix; no fake host implementation |
| `send.forward`, `send.hybrid`, `send.command`, `send.custom` | R/I | Rejected unless the host can preserve segment semantics; never downgraded to text |
| `component.get_plugin_config_schema`, `component.enable`, `component.disable` | I | MoFox exposes no matching public schema query or component state API |
| `MessageGateway`, `LLMProvider`, `HomeCard`, `WorkflowStep`, unsupported hooks | R | Unit admission rejects the declaration |

The adapter does not inject a local `maibot_sdk` module. It requires the official package and checks its version/export contract before loading a unit.

## Validation note

- The bundled `wife_plugin` is included in existing bridge integration coverage.
- `maibot-plugin-sdk==2.17.0` is installed from the pinned local upstream tag snapshot for validation.
- Component enable/disable and plugin configuration schema queries are explicitly unsupported because the host has no matching public APIs. Component plugin listing/load/unload/reload is mapped only to the host public plugin API.
