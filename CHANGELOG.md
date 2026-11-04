# Changelog

Notable changes to AetherLink. The Release workflow turns the **Unreleased** section into the GitHub release notes, so add entries there as you go.

## [Unreleased]

## [0.2.0] - 2026-11-04

### Fixes you need
- Works with the current MCP Python SDK environment: `mcp` is pinned below 2.0, which removed the server API this release uses. 0.1.3 fails to start on a fresh install.
- `pyrtlsdr` pinned below 0.4 so RTL-SDR support loads against the stock Osmocom driver (Homebrew `librtlsdr`).
- ADS-B and AIS decoding libraries are core dependencies; a plain `pip install aetherlink` has everything.

### New
- Built-in POCSAG pager decoder and built-in AIS receiver: no multimon-ng or AIS-catcher required (still selectable via `engine`).
- Aircraft positions from CPR frame pairs, with distance and bearing; nearest aircraft first.
- `satellite_predict_passes`: Meteor-M pass times for your location from live Celestrak elements.
- `pager_find_frequencies`: scans the paging bands with max-hold for active channels.
- `sdr_list_devices`, `sdr_set_sample_rate`, `sdr_set_ppm`, `sdr_set_bias_tee`.
- Meteor-M decoding runs in the background with `satellite_get_status` / `satellite_stop_decoding`.
- The first RTL-SDR is opened automatically; the receiver location is detected automatically (and corrected from decoded aircraft if an IP/VPN guess is wrong).
- ADS-B falls back to `rtl_adsb` when dump1090 hears nothing; default gain is maximum with dump1090-fa adaptive gain.
- Structured JSON output on list-returning tools; gap-free IQ and audio capture; vectorised audio demodulation with 15 kHz FM low-pass.
- Recordings default to `~/AetherLink/recordings` (`AETHERLINK_RECORDINGS_DIR` to override).

### Verified
- Live on an RTL-SDR: device control, spectrum, audio and IQ recording, ADS-B (8 aircraft in 2 minutes, positions cross-checked against a public feed), rtl_433 and SatDump lifecycles.
- Hardware-free: 46 end-to-end tool checks with a mock radio replaying recorded IQ.

### Not yet verified on real signals
- Built-in POCSAG and AIS decoders (no pager or ship traffic was in range during testing; both decode synthetic signals end to end).
- Meteor-M imagery during an actual pass.
- HackRF (driver targets libhackrf directly; no board was available).

## [0.1.3] - 2026-05-08
- ADS-B workflow ported into the MCP server; installer additions. See the GitHub release for details.

## [0.1.2] - 2026-04-25
- Lightweight CLI entry point, cross-platform install scripts.

## [0.1.1] - 2026-04-25
- Initial PyPI releases.
