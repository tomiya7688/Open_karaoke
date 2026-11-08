# Audio import and normalization

> 実装済みはRust CoreのFFmpeg正規化と、それを実行する `audio.import` Core Jobです。
> GUIから楽曲登録・解析Jobまで一度に進む完成済みのインポート画面（#15）を意味しません。
> 残る結合は [Current status](current_status.md) を参照してください。

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

## `audio.import` Core Job

認証付き `POST /jobs` に、利用者が選んだローカル元ファイルの絶対パスを渡す。

```json
{"kind":"audio.import","audio_import":{"source_path":"C:/Music/song.mp3"}}
```

作成前に検査する: 絶対パス・対応拡張子・通常ファイル（400 `audio_source_invalid` /
`audio_unsupported_input`、存在しなければ404 `audio_source_not_found`）。未知フィールドは拒否、
正規化設定のないCoreは503。Jobはblocking workerでFFmpegを実行し、stageは `audio_import.normalizing`。

成功時のartifactsは `imports/<job_id>/original_48k.wav` と `imports/<job_id>/normalized.json`
（sample_rate・channels・整数duration_samples）。前者をそのまま `analysis.song` の
`input_artifact` に渡す。元ファイルのパスは成果物に記録しない。

出力は `imports/.staging-<job_id>/` で作り、検査後に1回のrenameで公開する。失敗・キャンセルでは
stagingを消し、artifactsは空。キャンセルは実行中のffmpeg/ffprobeをkillし、song.importと同じく
公開確定とatomicに順序付けする。errorは安定コードで始まる:
`audio_tool_unavailable`（ffmpeg/ffprobe未導入・起動不可）、`audio_ffmpeg_license_policy`、
`audio_tool_failed`、`audio_invalid_probe`（0 sampleを含む）、`audio_import_io`。

Coreは `OPEN_KARAOKE_FFMPEG` / `OPEN_KARAOKE_FFPROBE`（既定はPATH上の `ffmpeg` / `ffprobe`）を使う。
Job契約テストは正規化処理を差し替えて行い、実FFmpegでの変換はUnverified（LGPL版の同梱は#39）。
