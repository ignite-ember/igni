# igni app

igni's desktop app and its web UI. The desktop app (`clients/tauri`, Tauri)
starts igni's backend and loads the web UI (`clients/web`, React), which is
also igni's web client.

| | |
|---|---|
| Download | [Releases](https://github.com/ignite-ember/igni-app/releases): `.dmg`, `.msi`, `.AppImage`; installed apps update themselves |
| Backend | [igni-core](https://github.com/ignite-ember/igni-core), installed on first launch as `ignite-ember==<this app's version>` |
| SDKs | [igni-sdk](https://github.com/ignite-ember/igni-sdk): the extension bridge, the React floor and the stylesheet this UI is drawn with |

The app and the backend are released together under one version:
[`igni-core.version`](igni-core.version) names it, and a release stops before
building anything unless igni-core has published that version.

## Working here

See [CONTRIBUTING.md](CONTRIBUTING.md). In short, with igni-sdk and igni-core
checked out beside this repository:

```bash
make install
make check
make help
```

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
