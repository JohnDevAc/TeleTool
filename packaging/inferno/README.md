# TeleTool Inferno Companion Package

`teletool-inferno` packages a pinned upstream Inferno-AoIP ALSA PCM and the
Inferno Statime clock fork for Raspberry Pi OS ARM64.

The package intentionally stays separate from the proprietary `teletool`
package. It is distributed through the same signed TeleTool APT repository so
fresh installs and Web updates can pull it as a normal package dependency.

Default runtime pieces:

- ALSA PCM: `teletool_inferno`
- ALSA config: `/etc/alsa/conf.d/60-teletool-inferno.conf`
- Clock service: `teletool-inferno-clock.service`
- Runtime clock socket: `/run/teletool-inferno/usrvclock.sock`

The clock service writes `/run/teletool-inferno/statime.toml` at start. By
default it uses the default-route network interface, then the first active
non-loopback interface, then `eth0`. Override with
`TELETOOL_INFERNO_INTERFACE` in `/etc/default/teletool-inferno`.
