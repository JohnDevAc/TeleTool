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

The TeleTool service permits realtime priority up to 81 so Inferno can schedule
its audio transmitter thread at its requested FIFO priority. The rest of the
application retains normal scheduling. Without this allowance, CPU load from
video processing can delay audio packets even while PTP remains synchronized.
For an active Inferno output, the following should show `flows TX` with class
`FF` and priority `81`:

```sh
ps -T -p "$(systemctl show teletool -p MainPID --value)" -o tid,comm,cls,rtprio
```

Inferno output always uses `alsasink sync=false`: the shared decoded-audio
handoff already paces samples, and Inferno consumes them against its PTP clock.
Scheduling them again at the ALSA sink can clip late samples into intermittent
or distorted audio even when network packets arrive on time. Existing saved
`lineout_sink_sync` settings still apply to other audio outputs; Inferno ignores
that setting and reports its effective `sink_sync` as `false` in audio status.

During package configuration, known source-install service and ALSA overrides
are moved to timestamped files under `/var/backups/teletool-inferno/`. This
allows the package-owned clock service and PCM definition to become active
without discarding prior configuration.
