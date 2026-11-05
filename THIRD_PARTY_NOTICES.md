# Third-Party Notices

> 現在は**公式配布物の台帳テンプレート**です。下表だけで全依存を網羅したわけではありません。
> 個別モデルの出典は [Stem provenance](docs/stem_separation.md)、
> [Whisper provenance](docs/lyric_transcription.md)、[Alignment provenance](docs/lyric_alignment.md)、
> `docs/licenses/` に記載しています。配布バイナリごとの依存台帳・NOTICE統合は
> [Issue #19](https://github.com/tomiya7688/Open_karaoke/issues/19) までに実施する必要があります。

This file tracks third-party software, models, runtimes, data, and other components distributed with or required by official Open_karaoke builds.

> This list is intentionally maintained alongside automated dependency/license inventory. Inclusion in source dependencies does not automatically mean inclusion in a distributed binary.

| Component | Type | Version | License | Commercial use | Redistribution | Included in official build | Notes |
|---|---|---|---|---|---|---|---|
| TBD | — | — | — | — | — | — | Complete remaining entries from the dependency inventory |
| [CPAL](https://github.com/RustAudio/cpal) | Rust audio I/O library (Windows backend) | 0.16.0 | Apache-2.0 | Yes | Yes (license/notice required) | Planned for Windows build | Source crate only; no ASIO; WASAPI shared; complete transitive inventory at release |
| [FFmpeg](https://ffmpeg.org/legal.html) | Media decode/normalize tool (`tools/ffmpeg/ffmpeg`, `ffprobe`) | Unverified (binary not yet selected) | LGPL-2.1-or-later required (`--enable-gpl` / `--enable-nonfree` rejected by Core) | Unverified until the exact build and its codec licenses are confirmed | Unverified | Planned; not yet in any official build | Slice 39.3 provides the checks only: no FFmpeg binary is committed. Per-binary `buildconf.txt` and `NOTICE.txt` (version, sha256, license) must match the `runtime-manifest.json` ffmpeg entry (`ffmpeg_bundle::verify_bundle`). Source offer / relinking obligations for the chosen build are Unverified. |
| [RustCrypto sha2](https://docs.rs/crate/sha2/0.10.9/source/Cargo.toml) | Streaming asset checksum library | 0.10.9 | MIT OR Apache-2.0 | Yes | Yes (chosen license obligations apply) | Core dependency | SHA-256 for verified SongStore imports. Exact version and transitives are recorded in Cargo.lock; no models or weights. Retain selected license/notice in the release inventory. |

## Review rules

Before a component is included in an official release:

1. Verify the license of the source code.
2. Verify the license/terms of model weights or bundled data separately.
3. Verify commercial-use permission.
4. Verify redistribution permission and attribution requirements.
5. Record any NOTICE/source-offer/relinking obligations.
6. Reject unclear or non-commercial-only components from official builds.
