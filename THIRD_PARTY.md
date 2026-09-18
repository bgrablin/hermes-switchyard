# Third-party references

This repository is original plugin work under the MIT license. It uses the documented Hermes Agent native plugin interface and its `PluginContext` registration contract. No upstream source code is vendored here.

The Hermes guidance reviewed for this plugin is:

- [Hermes plugin catalog guidance](https://hermes-agent.nousresearch.com/docs/user-guide/features/plugin-catalog)
- [Hermes Agent catalog policy at the reviewed upstream commit](https://github.com/NousResearch/hermes-agent/blob/8503ee4459316ce092b5d69b7d396c27aa03d0be/plugin-catalog/README.md)
- [Hermes plugin authoring guidance at the reviewed upstream commit](https://github.com/NousResearch/hermes-agent/blob/8503ee4459316ce092b5d69b7d396c27aa03d0be/plugins/AGENTS.md)
- [Hermes example plugin using the host-owned `ctx.llm` API](https://github.com/NousResearch/hermes-example-plugins/tree/main/plugin-llm-example)

Switchyard follows the native general-plugin contract: root `plugin.yaml`, root `__init__.py`, `register(ctx)`, explicit tool declarations, and host-owned context APIs. The text helper uses `ctx.llm`; it does not integrate with a private host or alter Hermes core. Catalog admission remains a separate human-reviewed exact-SHA process.

The client targets the documented OpenRouter Decisions endpoint and the Jev model aliases recorded in the plugin contract. OpenRouter and Jev are external services and names; no external service implementation is bundled here.

The tests and evaluation data are public synthetic fixtures authored for this repository. The implementation uses Python standard-library modules only and does not vendor third-party code.
