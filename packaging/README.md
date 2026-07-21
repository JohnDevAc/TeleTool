# TeleTool Debian Package and APT Repository

TeleTool is packaged for 64-bit Raspberry Pi OS based on Debian Trixie. The
binary package contains the application and a prebuilt ARM64 NDI®
`libgstndi.so`, so
an end user's Pi does not need Rust, Cargo, compilers, or GStreamer development
headers.

TeleTool is proprietary software made available free of charge for
non-commercial use. Commercial use requires a separate written licence. The
package carries the complete TeleTool terms, Debian copyright metadata, the
MPL-2.0 plugin source, and notices for every Rust dependency compiled into the
plugin.

Experimental Inferno-AoIP support is runtime interoperability with a
separately installed ALSA PCM. The TeleTool package does not ship Inferno-AoIP
source, binaries, services, or configuration.

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

The plugin build also creates `dist/gst-plugin-ndi-0.13.5.crate` and
`dist/gst-plugin-ndi-licenses/`. The package build intentionally fails if these
compliance artifacts are missing. The result is
`dist/teletool_<version>_arm64.deb`.

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
The build job creates unsigned repository artifacts and cannot access the APT
private key. Publication happens in a separate environment-gated job only after
the build and validation steps pass:

- A `dev` build waits for approval in `teletool-dev-apt`, signs and verifies the
  artifact, then commits `apt-repo-dev/` back to `dev`.
- A `v*` tag waits for approval in `teletool-stable-apt`, confirms the tag
  matches `VERSION`, signs and verifies the stable artifact, fast-forwards
  `main` to the tested tag, and atomically aligns `main` and `dev` on the stable
  publication commit.

Both jobs reject stale builds if their source branch advanced while approval
was pending. `scripts/sign_apt_repo.sh` verifies package identity, ARM64
architecture, version, indexed SHA-256, signing-key fingerprint, and both APT
signatures before publication. The ASCII-armoured private key is supplied only
through the Actions secret `TELETOOL_APT_GPG_PRIVATE_KEY`; the private key must
never be committed. The public key remains in each published repository.

This project publishes GitHub Pages from the `main` branch. Configure JohnDevAc
as a required reviewer on both publication environments so signed feeds cannot
change without an explicit approval.

Development packages use the separately signed `apt-repo-dev/` repository on
the `dev` branch with suite `dev` and Debian versions such as
`1.7.6+dev.<run>`. Package-managed units can switch between Main and Dev from
the System page; the updater rewrites only the TeleTool source and installs the
exact version advertised by the selected signed repository.

The published fresh-install command is:

```sh
wget -qO- https://johndevac.github.io/TeleTool/apt-repo/install.sh | sudo sh
```

Never commit the private APT signing key. Existing clients must continue to use
the same key for future repository updates.
