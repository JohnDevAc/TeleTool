# TeleTool

## Install with WGET

On a Raspberry Pi 5 running 64-bit Raspberry Pi OS Lite:

```sh
wget -qO- https://johndevac.github.io/TeleTool/apt-repo/install.sh | sudo sh
```

The installer adds the signed TeleTool package repository, installs TeleTool
and its dependencies, and displays the Web UI address when complete.

If prompted, download the Linux ARM64 NDI® SDK runtime from
[ndi.video](https://ndi.video/), then upload `libndi.so.6` using the guided
TeleTool setup page.

## What it does

TeleTool sends Tvheadend television services to NDI and can route the active
stream to a local USB line-level audio device. It provides a browser interface
for channel selection, TV setup, audio, system settings, updates, and managing
multiple TeleTool units.

Open the interface at the address shown by the installer, normally:

```text
http://<teletool-host>:8000/
```

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
