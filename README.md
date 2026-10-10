# noctalia-smart-storage

A [Noctalia](https://github.com/noctalia-dev/noctalia) v5 plugin source with one plugin: **[Smart Storage](smart-storage/)** — a storage cleaner for developer machines.

<!-- derpcat-support -->
<p align="center">
  <a href="https://www.patreon.com/derpcatmusic">
    <img src=".github/support-derpcat.svg" alt="Donate on Patreon" width="480">
  </a>
</p>
<!-- /derpcat-support -->

```sh
noctalia msg plugins source add derpcat git https://github.com/DerpcatMusic/noctalia-smart-storage
noctalia msg plugins enable derpcat/smart-storage
```

Then run the one-time helper setup (asks for sudo once) from the materialized plugin directory:

```sh
sh ~/.local/state/noctalia/plugins/materialized/derpcat/smart-storage/install.sh
```

See [smart-storage/README.md](smart-storage/README.md) for what it cleans and how it stays safe.
