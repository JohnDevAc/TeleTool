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

Application and installer versions are tracked separately in `VERSION` and
`INSTALLER_VERSION`. The installer version is displayed by the terminal UI and
stored in the package's `X-TeleTool-Installer-Version` control field.

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
terminal-based `install.sh` bootstrap. The bootstrap keeps APT output in
`/var/log/teletool-installer.log`, displays a full-screen percentage view, and
uses APT's status channel to advance through individual download, unpack,
configuration, and trigger actions. Individual package details stay in the log;
the terminal shows a simple patience message. It prints the Web UI address only
after package triggers and service checks finish. The progress and completion
screens use the TeleTool yellow/blue palette and a compact ASCII banner. A
single guarded APT repair pass handles transient dependency post-install
failures before the installer stops and presents the filtered error summary.

## GitHub publishing

The `Build TeleTool APT package` workflow uses GitHub's native ARM64 runner.
Tagged builds attach the `.deb` to a GitHub Release. If an ASCII-armoured
private key is supplied through the Actions secret
`TELETOOL_APT_GPG_PRIVATE_KEY`, the repository artifact is signed as well.

This project publishes GitHub Pages from the `main` branch. To release the APT
repository, build and sign `apt-repo/`, commit that directory, and keep `main`
and `dev` on the same tested release commit. The private signing key must remain
outside Git and the public key is published inside `apt-repo/`.

The published fresh-install command is:

```sh
curl -fsSL https://johndevac.github.io/teletwat/apt-repo/install.sh | sudo sh
```

Never commit the private APT signing key. Existing clients must continue to use
the same key for future repository updates.
