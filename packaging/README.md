# TeleTool Debian Package and APT Repository

TeleTool is packaged for 64-bit Raspberry Pi OS based on Debian Trixie. The
binary package contains the application and a prebuilt ARM64 `libgstndi.so`, so
an end user's Pi does not need Rust, Cargo, compilers, or GStreamer development
headers.

## Package layout

- Application: `/usr/lib/teletool`
- Editable configuration: `/var/lib/teletool/config.json`
- Uploaded SDK staging file: `/var/lib/teletool/libndi.so.6`
- Installed SDK runtime: `/usr/local/lib/libndi.so.6`
- GStreamer plugin: `/usr/lib/aarch64-linux-gnu/gstreamer-1.0/libgstndi.so`
- Service: `teletool.service`, running as the `teletool` system user

The package configures local-only Tvheadend administration, seeds a DVB-T
network, installs the fixed NDI validation helper, and provides the minimum
sudo rules used by the System page.

## Build on ARM64

Install the build dependencies, then run:

```sh
scripts/build_gst_ndi_plugin.sh dist/libgstndi.so
TELETOOL_GST_NDI_PLUGIN="$PWD/dist/libgstndi.so" scripts/build_deb.sh dist
```

The result is `dist/teletool_<version>_arm64.deb`.

## Build a repository

`build_apt_repo.sh` needs `dpkg-dev`, `apt-utils`, `xz-utils`, and GnuPG. Use a
dedicated repository-signing key and keep its private key outside this Git
repository:

```sh
export TELETOOL_GPG_KEY=<signing-key-fingerprint>
scripts/build_apt_repo.sh dist apt-repo
```

This creates the standard `dists/stable/main/binary-arm64` and `pool` layout,
signed `InRelease`/`Release.gpg` metadata, the public archive keyring, and the
terminal-based `install.sh` bootstrap.

## GitHub publishing

The `Build TeleTool APT package` workflow uses GitHub's native ARM64 runner. Add
an ASCII-armoured private signing key as the repository Actions secret
`TELETOOL_APT_GPG_PRIVATE_KEY`, select GitHub Actions as the Pages publishing
source, then run the workflow or push a version tag. Tagged builds also attach
the `.deb` to a GitHub Release.

The published fresh-install command is:

```sh
curl -fsSL https://johndevac.github.io/teletwat/install.sh | sudo sh
```

Never commit the private APT signing key. Existing clients must continue to use
the same key for future repository updates.
