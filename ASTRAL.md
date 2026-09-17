# Astral packaging

This repository is configured as an Astral source package. Astral reads
`package.toml`, builds the Rust project, and installs the declared `neofetch`
binary from `target/release/neofetch`.

## Install with Astral

From a local checkout:

```bash
python astral.py install neofetch --link https://raw.githubusercontent.com/anorberg001-web/neofetch_astral/main
```

Or publish the repository under a configured Astral mirror and run:

```bash
astral install neofetch
```

Astral uses the following package contract:

- `package.toml` declares the package name, version, binary, and build command.
- `dependencies.toml` declares Astral dependencies under `[dependencies]`.
- `target/release/neofetch` is the build output copied to the configured binary directory.

To keep the unpacked source after installation:

```bash
astral install neofetch --keep-source
```

## Build manually

```bash
cargo build --release
./target/release/neofetch
```

The original project documentation and platform notes are preserved in the
repository history; this file focuses on the Astral package contract.
