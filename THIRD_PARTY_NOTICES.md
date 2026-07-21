# Third-party notices

TeleTool contains and interoperates with third-party software. TeleTool's
proprietary licence does not replace or restrict the licences below.

## GStreamer NDI® plugin

TeleTool's Debian package contains `gst-plugin-ndi` version 0.13.5 from the
GStreamer `gst-plugins-rs` project.

- Licence: Mozilla Public License 2.0 (`MPL-2.0`)
- Corresponding source: <https://static.crates.io/crates/gst-plugin-ndi/gst-plugin-ndi-0.13.5.crate>
- Upstream project: <https://gitlab.freedesktop.org/gstreamer/gst-plugins-rs>
- Source archive SHA-256: `ec8417e75002857f4c8e8fd2f2f1a7521937eaac3de264f7bb6904a0d22cba23`
- Licence text: <https://www.mozilla.org/MPL/2.0/>

The exact source archive and the licence files for Rust packages compiled into
`libgstndi.so` are included in the Debian package under
`/usr/share/doc/teletool/third-party/`.

The plugin was initially developed by Teltek and funded by the University of
the Arts London and the University of Manchester. Its upstream acknowledgements
also credit Sebastian Dröge and the other GStreamer contributors.

## NDI runtime and trademark

The proprietary NDI runtime is not distributed as part of TeleTool. It must be
obtained separately by the user from <https://ndi.video/> and remains subject to
the NDI SDK licence applicable to that download.

NDI® is a registered trademark of Vizrt NDI AB. TeleTool is an independent
project and is not affiliated with or endorsed by Vizrt NDI AB.

## Optional Inferno-AoIP interoperability

TeleTool can detect and use the optional `teletool-inferno` companion package
as an experimental network audio output. The main `teletool` Debian package
does not include Inferno-AoIP source code, binaries, ALSA modules, Statime
builds, service files, or configuration. The separate `teletool-inferno`
package is published in the same signed TeleTool APT repository and includes a
pinned upstream Inferno ALSA PCM, a pinned Inferno Statime fork build, service
configuration, and corresponding source/licence material under
`/usr/share/doc/teletool-inferno/`.

- Licence: GNU General Public License v3 or later (`GPL-3.0-or-later`) or GNU
  Affero General Public License v3 or later (`AGPL-3.0-or-later`), at the
  user's option under upstream terms
- Upstream project: <https://github.com/teodly/inferno>
- Primary upstream repository: <https://gitlab.com/lumifaza/inferno>

Inferno-AoIP is an unofficial Dante-compatible implementation and is not
affiliated with or endorsed by Audinate. TeleTool's proprietary licence does
not replace, narrow, or grant rights to Inferno-AoIP.

## Separately installed dependencies

TeleTool uses FastAPI, Uvicorn, Pydantic, Requests, urllib3, PyGObject,
GStreamer, Tvheadend, Avahi, FFmpeg-related GStreamer components, ALSA, and other
packages supplied separately by Python or Raspberry Pi OS/Debian. Those
packages retain their own copyright and licence terms; their installed package
documentation contains the corresponding notices.

Tvheadend, GStreamer, Raspberry Pi, Python, FastAPI, and other product names are
the property of their respective owners. Their mention describes compatibility
and does not imply endorsement.

## TeleTool artwork

The TeleTool name, logo, and original artwork are copyright © 2026 John
Lightfoot. They are proprietary TeleTool material and are not granted for reuse
or redistribution.
