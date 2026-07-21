# TeleTool

## Install with WGET

On a Raspberry Pi 5 running 64-bit Raspberry Pi OS Lite:

```sh
wget -qO- https://johndevac.github.io/TeleTool/apt-repo/install.sh | sudo sh
```

The installer asks which branch to install. Press Enter for Main, or choose Dev
when testing development builds.

For unattended installs:

```sh
wget -qO- https://johndevac.github.io/TeleTool/apt-repo/install.sh | sudo sh -s -- main
wget -qO- https://johndevac.github.io/TeleTool/apt-repo/install.sh | sudo sh -s -- dev
```

The installer adds the selected signed TeleTool package repository, installs
TeleTool and its dependencies, and displays the Web UI address when complete.

If prompted, download the Linux ARM64 NDI® SDK runtime from
[ndi.video](https://ndi.video/), then upload `libndi.so.6` using the guided
TeleTool setup page.

## What it does

TeleTool sends TV services to NDI and can route the active stream to a local
USB line-level audio device or to the optional `teletool-inferno` network audio
companion package. It provides a browser interface for channel selection, TV
setup, audio, system settings, updates, and managing multiple TeleTool units.

Open the interface at the address shown by the installer, normally:

```text
http://<teletool-host>:8000/
```

## Inferno-AoIP

Experimental Inferno-AoIP support is supplied through the optional
`teletool-inferno` companion package from the signed TeleTool APT repository.
That package installs a pinned upstream Inferno ALSA PCM, the Statime clock
service it needs, and source/licence material under
`/usr/share/doc/teletool-inferno/`.

## Documentation

- [api.md](API.md) — API reference
- [Licence](License.md)
- [Third-party notices](THIRD_PARTY_NOTICES.md)

## Licence

Copyright © 2026 John Lightfoot. All rights reserved.

TeleTool is proprietary software made available free of charge for
non-commercial use. Commercial use requires a separate written licence. See
[License.md](License.md) for the complete terms.

TeleTool is an independent project and is not affiliated with or endorsed by
Vizrt NDI AB. NDI® is a registered trademark of Vizrt NDI AB.
