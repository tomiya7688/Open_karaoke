# Third-Party Notices

This file tracks third-party software, models, runtimes, data, and other components distributed with or required by official Open_karaoke builds.

> This list is intentionally maintained alongside automated dependency/license inventory. Inclusion in source dependencies does not automatically mean inclusion in a distributed binary.

| Component | Type | Version | License | Commercial use | Redistribution | Included in official build | Notes |
|---|---|---|---|---|---|---|---|
| TBD | — | — | — | — | — | — | Add entries when dependencies are selected |
| [CPAL](https://github.com/RustAudio/cpal) | Rust audio I/O library (Windows backend) | 0.16.0 | Apache-2.0 | Yes | Yes (license/notice required) | Planned for Windows build | Source crate only; no ASIO; WASAPI shared; complete transitive inventory at release |

## Review rules

Before a component is included in an official release:

1. Verify the license of the source code.
2. Verify the license/terms of model weights or bundled data separately.
3. Verify commercial-use permission.
4. Verify redistribution permission and attribution requirements.
5. Record any NOTICE/source-offer/relinking obligations.
6. Reject unclear or non-commercial-only components from official builds.
