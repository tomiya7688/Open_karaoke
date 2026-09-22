# Audio import and normalization

## Internal format

Supported source formats for the initial product requirement:

- WAV
- AAC
- MP3
- MP4

The normalized artifact is always WAV, 48,000 Hz, stereo, float32 PCM (`pcm_f32le`), with an integer sample-count timeline.

Video input maps only the first audio stream and disables video output.

## FFmpeg process boundary

Rust Core invokes `ffmpeg` and `ffprobe` as external tools rather than linking their libraries into the Rust binary.

The normalization command removes source metadata and uses bit-exact flags so repeated runs with the same FFmpeg build and input are stable.

Output is written to a temporary WAV and renamed into place only after probing and validation succeeds.

## Distribution license policy

The selected distribution policy is an LGPL-compatible FFmpeg build.

FFmpeg upstream states that most FFmpeg code is LGPL 2.1+ by default, while `--enable-gpl` activates GPL components and `--enable-nonfree` can make a resulting binary unredistributable.

Core executes `ffmpeg -buildconf` and rejects configurations containing:

- `--enable-gpl`
- `--enable-nonfree`

The packaged FFmpeg build, enabled external libraries, source-offer requirements, notices, and exact license text must still be recorded in the release dependency/license inventory.

## Exact duration

After conversion, `ffprobe` reads the first audio stream's `sample_rate`, `channels`, and `duration_ts`.

For normalized PCM WAV, `duration_ts` is used as integer `duration_samples` in Song Data. Floating-point seconds are not canonical.

## Song Data integration

`apply_metadata` updates `sample_rate`, `duration_samples`, and `files.original`.

Later Job API integration can move the blocking FFmpeg process into a worker without changing this normalization contract.
