# Shared Media Timing

## Output Model

TV is ingested, demuxed and decoded once. Decoded audio is normalized to
interleaved 48 kHz stereo F32 PCM by default, then shared with NDI and the
independently controlled audio output. Audio output no longer opens a second
TV subscription. The card's native generator feeds these same output chains;
it is not encoded and looped through TV software.

Both raw branches use `identity single-segment=true` to convert source PTS/DTS
to segment running-time. Their native clock synchronization paces file/HTTP
decoders before the bounded output queues. Both pipelines use the system
monotonic clock. This preserves source A/V offsets rather than independently
setting the first audio and video timestamps to zero. See GStreamer's
[clock and segment model](https://gstreamer.freedesktop.org/documentation/application-development/advanced/clocks.html).

The output schedule is:

```text
NDI video and embedded audio = primary base time + media running-time
                              + shared baseline + configured NDI delay
Separate audio              = primary base time + media running-time
                              + shared baseline
```

The shared baseline includes the source and native combiner/sink minimum
latencies plus scheduling allowance. Initial automatic latency negotiation is
retained until output has begun, then the baseline is set. NDI's configured
delay is explicitly excluded from the separate-audio schedule. The local
appsrc PTS is mapped into its own pipeline's base time, without changing the
original NDI buffer header or copying PCM payload. Python's boxed
`Gst.Buffer.copy()` is not a writable-header copy; `copy_region` is required.

Global live-pipeline latency applies at synchronized sinks, so merely placing
a synchronized audio bridge before a delayed queue is not sufficient. The
local pipeline receives its own latency and clock mapping. See GStreamer's
[latency design](https://gstreamer.freedesktop.org/documentation/additional/design/latency.html).

The existing `ndi_delay_ms` setting and its min/max clamps now apply to the
card as well as TV. Queue capacity/headroom and per-request extra buffering
also follow the same calculation. The card remains 1080p60 by default, with
native clock animation and a 1 kHz pulse once per second. Motion is installed
before PLAYING. Conversion to the configured NDI format (normally UYVY) occurs
before the delayed video queue. Progressive material keeps progressive caps;
only mixed-mode decoder caps receive the existing interleaved compatibility
override.

## Isolation And Bounds

- The producer offers native PCM references to a byte-, time- and count-bounded
  handoff. It never calls an audio sink and never waits for the consumer lock.
- A separate feeder maps timestamps and pushes to a bounded, nonblocking,
  downstream-leaky appsrc in the audio pipeline. No blocking audio tee is added
  to NDI. At defaults each handoff/appsrc queue holds at most about 75 KiB of
  stereo F32 PCM, with one in-flight buffer per stage.
- Slow consumers discard old audio and mark discontinuity. Turning audio off
  empties the handoff. Audio starts only when decoded audio and timing are ready.
- NDI raw queues retain their time limits. Video additionally has a negotiated
  frame-count bound; audio has a format-derived byte bound, so unusable source
  timestamps cannot cause unlimited queue growth.
- Audio owns its state transitions and teardown worker. A previous stalled
  worker must finish before a new output can replace it. PTP unready blocks
  Inferno start, and PTP loss stops only the separate audio output.
- Native plugin process crashes are not isolated: the application still uses
  one process. These changes isolate normal sink errors, back-pressure and
  stalled teardown, not segmentation faults in third-party native code.

## Calibration

For USB/local outputs, keep `lineout_sink_sync=true` for clocked alignment.
Inferno always bypasses timestamp synchronization at the ALSA sink: the shared
handoff paces PCM and Inferno consumes it against the network PTP clock. Applying
the same timestamps again at this sink was observed to break the test-card
pulse into intermittent audio. Its actual output latency includes ALSA and
network buffering; the schedule above describes the handoff, not desk playback.
The service permits the Inferno transmitter to request FIFO priority 81 so
video processing cannot routinely delay its packet transmission.

Set desk/DSP audio delay while watching the
actual NDI display and listening through the actual audio endpoint. Keep the
same NDI delay, audio endpoint, receiver/display processing and output format
when changing to TV.

The shared model removes the former card/TV routing mismatch. It does not fix
broadcast lip-sync faults or cancel decoder, AVIO/Inferno, network, desk or
display latency. Frame-rate changes and deinterlacing can alter receiver and
display latency. Physical calibration is still required.

## Verification

`scripts/check_media_timing.py` uses real GStreamer synchronized sink handoffs,
after clock waiting, rather than measuring arrival at a sink pad. It compares
100 and 500 ms using generated A/V and a synchronized Theora/Vorbis fixture
through URI decoding, at matching 320x180/60 UYVY output. It covers late audio
attachment, stop/restart, buffer-header immutability, queue limits, stalled
teardown, EOS and progressive/mixed caps. Existing lifecycle checks cover
startup cancellation and rapid starts/stops.

Measured locally on GStreamer 1.24.2: NDI-equivalent video minus separate audio
was approximately 100 and 500 ms in both source modes, changing by 400 ms.
Embedded A/V scheduling differed by less than 1 ms in these short controlled
tests. These are software sink-boundary measurements, not screen/PA accuracy.

`scripts/measure_media_device.py` is an explicitly opt-in hardware check. It
pauses and restores an active installed test card, including output settings.
On 192.168.0.31 with GStreamer 1.26.2, a temporary candidate using the actual
NDI sink sustained 1080p60 multicast with zero reported NDI drops at both
delays, with separate audio off/on. Missing ALSA hardware, PTP-unready and
simulated PTP loss did not stop NDI. The installed Main release and its
original stream/settings were restored after each test.

Short candidate-process samples (10 seconds per measurement, not a soak):

| Delay | Output | CPU, % of one core | RSS, KiB |
| --- | --- | ---: | ---: |
| 100 ms | NDI only | 263.2 | 155808 |
| 100 ms | NDI + clocked test sink | 266.4 | 156400 |
| 500 ms | NDI only | 267.4 | 293936 |
| 500 ms | NDI + clocked test sink | 270.5 | 294064 |

The 500 ms UYVY video queue held about 120 MB (115 MiB) in the sample. RSS
includes native pools and allocator retention; queue capacity is not occupancy.
ALSA null also passed routing/lifecycle tests but consumed roughly an extra
core in this environment, so it is not used to estimate physical audio-device
CPU cost. The clocked test sink measures handoff/conversion overhead only.

At test time the unit had no mapped TV channels, no USB audio device and no
ready PTP leader. Live RF/broadcast comparison, real Dante delivery, receiver
screen/PA alignment, and an extended soak remain site acceptance checks. No
measured percentage reduction in live TV decoding load is claimed.

On 192.168.20.105, live Dante diagnosis found two independent faults: the
service's default realtime-priority limit blocked Inferno's FIFO request, and
ALSA timestamp synchronization broke up the paced PCM. After permitting priority
81 and bypassing sink synchronization, a 30-second packet capture contained the
expected tone pulse every second with no capture drops or transmit-timing
warnings. The receiver subsequently reported zero late packets over 21 minutes
17 seconds, with a 4.6 ms peak against its 10 ms receive limit. These observations
cover the test card and this network; they do not establish long-term clock-drift
behavior or physical A/V alignment for all sources.
