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
- Runtime observation socket: `/run/teletool-inferno/observe.sock`

The clock service writes `/run/teletool-inferno/statime.toml` at start. By
default it uses the default-route network interface, then the first active
non-loopback interface, then `eth0`. Override with
`TELETOOL_INFERNO_INTERFACE` in `/etc/default/teletool-inferno`.

TeleTool runs the PTPv1 clock in follower-only mode and reads its port state
from the local observation socket. Inferno audio is available only while the
clock is synchronized to a network grandmaster or primary leader. If no leader
is present, the Web UI marks the output unavailable instead of starting an ALSA
pipeline that will time out.

During package configuration, known source-install service and ALSA overrides
are moved to timestamped files under `/var/backups/teletool-inferno/`. This
allows the package-owned clock service and PCM definition to become active
without discarding prior configuration.
