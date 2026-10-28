# Third-party components

This repository distributes the workflow's Python/PowerShell code, documentation, and synthetic tests. It does not redistribute QQ, SnowLuma binaries, native bindings, Node.js, login databases, or runtime caches.

- QQ is a Tencent product installed and logged into separately by the operator.
- SnowLuma runtime v2.1.1 comes from [SnowLuma's official release](https://github.com/SnowLuma/SnowLuma/releases/tag/v2.1.1). Its Windows full archive includes Node.js and native components. The installer pins the original archive URL and SHA-256 in runtime.lock.json, and leaves the release's EULA.md and PRIVACY.md available for review. Upstream terms govern those components; this repository's MIT license does not relicense them.
- Codex and the Google Calendar/Gmail connectors are separately provided services requiring the operator's own account connections and permissions. No service credentials or subscriptions are bundled.

The upstream release page states that accepting the EULA and privacy policy is required for unattended operation. This workflow asks the operator before enabling those acceptance flags. Selected message analysis and resulting calendar synchronization have separate data-processing scopes described in README.md.
