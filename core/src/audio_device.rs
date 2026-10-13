//! CPAL audio device boundary. The initial production backend is Windows/WASAPI shared mode.
//!
//! Only control threads enumerate, build, play and pause streams. The device callbacks call
//! the allocation-free `AudioIo` methods and never take a mutex or perform disk I/O.

use serde::Serialize;

use crate::audio_engine::AudioIo;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum DeviceDirection {
    Output,
    Input,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WasapiMode {
    Shared,
    Exclusive,
}

#[derive(Debug, Clone, Serialize)]
pub struct AudioDeviceInfo {
    pub name: String,
    pub direction: DeviceDirection,
    pub is_default: bool,
    pub supports_48khz: bool,
    pub host: String,
}

/// Returns a fresh device snapshot; names are display labels, not stable device identifiers.
///
/// # Errors
/// Reports unavailable devices and CPAL host query errors rather than fabricating a device.
pub fn enumerate_devices() -> Result<Vec<AudioDeviceInfo>, String> {
    platform::enumerate_devices()
}

#[cfg(windows)]
mod platform {
    use cpal::{
        BufferSize, Device, Host, SampleFormat, SampleRate, Stream, StreamConfig,
        SupportedStreamConfigRange,
        traits::{DeviceTrait, HostTrait, StreamTrait},
    };

    use crate::audio_engine::SAMPLE_RATE;

    use super::{AudioDeviceInfo, AudioIo, DeviceDirection, WasapiMode};

    fn host() -> Result<Host, String> {
        cpal::host_from_id(cpal::HostId::Wasapi).map_err(|error| error.to_string())
    }

    fn formats(
        device: &Device,
        direction: DeviceDirection,
    ) -> Result<Vec<SupportedStreamConfigRange>, String> {
        match direction {
            DeviceDirection::Output => device
                .supported_output_configs()
                .map(|ranges| ranges.collect())
                .map_err(|error| error.to_string()),
            DeviceDirection::Input => device
                .supported_input_configs()
                .map(|ranges| ranges.collect())
                .map_err(|error| error.to_string()),
        }
    }

    fn supports_48khz(ranges: &[SupportedStreamConfigRange]) -> bool {
        ranges.iter().any(|range| {
            range.min_sample_rate().0 <= SAMPLE_RATE
                && range.max_sample_rate().0 >= SAMPLE_RATE
                && range.channels() > 0
                && matches!(
                    range.sample_format(),
                    SampleFormat::F32 | SampleFormat::I16 | SampleFormat::U16
                )
        })
    }

    fn configure(
        device: &Device,
        direction: DeviceDirection,
    ) -> Result<(StreamConfig, SampleFormat), String> {
        let candidates = formats(device, direction)?;
        let selected = candidates
            .into_iter()
            .filter(|range| {
                range.channels() > 0
                    && range.min_sample_rate().0 <= SAMPLE_RATE
                    && range.max_sample_rate().0 >= SAMPLE_RATE
                    && matches!(
                        range.sample_format(),
                        SampleFormat::F32 | SampleFormat::I16 | SampleFormat::U16
                    )
            })
            .min_by_key(|range| {
                let sample_rank = match range.sample_format() {
                    SampleFormat::F32 => 0,
                    SampleFormat::I16 => 1,
                    SampleFormat::U16 => 2,
                    _ => 3,
                };
                let preferred_channels = match direction {
                    DeviceDirection::Output => 2,
                    DeviceDirection::Input => 1,
                };
                let channel_rank = range.channels().abs_diff(preferred_channels);
                (sample_rank, channel_rank)
            })
            .ok_or_else(|| "Device does not support a 48 kHz PCM stream".to_owned())?;
        Ok((
            StreamConfig {
                channels: selected.channels(),
                sample_rate: SampleRate(SAMPLE_RATE),
                buffer_size: BufferSize::Default,
            },
            selected.sample_format(),
        ))
    }

    fn find_device(
        host: &Host,
        direction: DeviceDirection,
        selected_name: Option<&str>,
    ) -> Result<Option<Device>, String> {
        if let Some(name) = selected_name {
            let matches: Vec<Device> = match direction {
                DeviceDirection::Output => host
                    .output_devices()
                    .map_err(|error| error.to_string())?
                    .filter(|device| device.name().is_ok_and(|label| label == name))
                    .collect(),
                DeviceDirection::Input => host
                    .input_devices()
                    .map_err(|error| error.to_string())?
                    .filter(|device| device.name().is_ok_and(|label| label == name))
                    .collect(),
            };
            return match matches.len() {
                0 => Err(format!("Audio device not found: {name}")),
                1 => Ok(matches.into_iter().next()),
                _ => Err(format!("Audio device name is ambiguous: {name}")),
            };
        }
        Ok(match direction {
            DeviceDirection::Output => host.default_output_device(),
            DeviceDirection::Input => host.default_input_device(),
        })
    }

    fn describe(
        device: &Device,
        direction: DeviceDirection,
        default_name: Option<&String>,
    ) -> Result<AudioDeviceInfo, String> {
        let name = device.name().map_err(|error| error.to_string())?;
        Ok(AudioDeviceInfo {
            is_default: default_name == Some(&name),
            supports_48khz: supports_48khz(&formats(device, direction)?),
            name,
            direction,
            host: "wasapi".to_owned(),
        })
    }

    pub(super) fn enumerate_devices() -> Result<Vec<AudioDeviceInfo>, String> {
        let host = host()?;
        let output_default = host
            .default_output_device()
            .and_then(|device| device.name().ok());
        let input_default = host
            .default_input_device()
            .and_then(|device| device.name().ok());
        let mut devices = Vec::new();
        for device in host.output_devices().map_err(|error| error.to_string())? {
            devices.push(describe(
                &device,
                DeviceDirection::Output,
                output_default.as_ref(),
            )?);
        }
        for device in host.input_devices().map_err(|error| error.to_string())? {
            devices.push(describe(
                &device,
                DeviceDirection::Input,
                input_default.as_ref(),
            )?);
        }
        Ok(devices)
    }

    fn output_stream(
        device: &Device,
        config: &StreamConfig,
        format: SampleFormat,
        io: &AudioIo,
    ) -> Result<Stream, String> {
        let channels = usize::from(config.channels);
        let data_io = io.clone();
        let error_io = io.clone();
        let stream = match format {
            SampleFormat::F32 => device.build_output_stream(
                config,
                move |buffer: &mut [f32], _| data_io.render(buffer, channels),
                move |_| error_io.mark_output_device_error(),
                None,
            ),
            SampleFormat::I16 => device.build_output_stream(
                config,
                move |buffer: &mut [i16], _| data_io.render(buffer, channels),
                move |_| error_io.mark_output_device_error(),
                None,
            ),
            SampleFormat::U16 => device.build_output_stream(
                config,
                move |buffer: &mut [u16], _| data_io.render(buffer, channels),
                move |_| error_io.mark_output_device_error(),
                None,
            ),
            _ => return Err("Unsupported output sample format".to_owned()),
        };
        stream.map_err(|error| error.to_string())
    }

    fn input_stream(
        device: &Device,
        config: &StreamConfig,
        format: SampleFormat,
        io: &AudioIo,
    ) -> Result<Stream, String> {
        let channels = usize::from(config.channels);
        let data_io = io.clone();
        let error_io = io.clone();
        let stream = match format {
            SampleFormat::F32 => device.build_input_stream(
                config,
                move |buffer: &[f32], _| data_io.capture(buffer, channels),
                move |_| error_io.mark_input_device_error(),
                None,
            ),
            SampleFormat::I16 => device.build_input_stream(
                config,
                move |buffer: &[i16], _| data_io.capture(buffer, channels),
                move |_| error_io.mark_input_device_error(),
                None,
            ),
            SampleFormat::U16 => device.build_input_stream(
                config,
                move |buffer: &[u16], _| data_io.capture(buffer, channels),
                move |_| error_io.mark_input_device_error(),
                None,
            ),
            _ => return Err("Unsupported input sample format".to_owned()),
        };
        stream.map_err(|error| error.to_string())
    }

    /// Owns physical CPAL streams, which must outlive their callbacks.
    pub struct AudioDeviceStreams {
        audio: AudioIo,
        output: Stream,
        input: Option<Stream>,
    }

    impl AudioDeviceStreams {
        /// Open the selected 48 kHz WASAPI shared devices (default if name is None).
        /// An absent default microphone is allowed; a specified missing microphone is an error.
        ///
        /// # Errors
        /// Reports devices, invalid configurations and stream initialization failures.
        pub fn open(
            output_name: Option<&str>,
            input_name: Option<&str>,
            mode: WasapiMode,
            capacity_frames: usize,
        ) -> Result<Self, String> {
            if mode == WasapiMode::Exclusive {
                return Err(
                    "Exclusive WASAPI needs explicit low-level latency and ownership support; only shared mode is implemented"
                        .to_owned(),
                );
            }
            let host = host()?;
            let output_device = find_device(&host, DeviceDirection::Output, output_name)?
                .ok_or_else(|| "No output audio device found".to_owned())?;
            let input_device = find_device(&host, DeviceDirection::Input, input_name)?;
            let audio = AudioIo::new(capacity_frames).map_err(|error| error.to_string())?;
            let (output_config, output_format) =
                configure(&output_device, DeviceDirection::Output)?;
            let output = output_stream(&output_device, &output_config, output_format, &audio)?;
            let input = input_device
                .map(|device| {
                    let (input_config, input_format) = configure(&device, DeviceDirection::Input)?;
                    input_stream(&device, &input_config, input_format, &audio)
                })
                .transpose()?;
            Ok(Self {
                audio,
                output,
                input,
            })
        }

        #[must_use]
        pub fn audio(&self) -> AudioIo {
            self.audio.clone()
        }

        /// # Errors
        /// Returns a CPAL device/stream error without advancing the playback clock.
        pub fn play(&self) -> Result<(), String> {
            self.output.play().map_err(|error| error.to_string())?;
            self.audio.set_playing(true);
            Ok(())
        }

        /// # Errors
        /// Returns a CPAL stream pause error.
        pub fn pause(&self) -> Result<(), String> {
            self.output.pause().map_err(|error| error.to_string())?;
            self.audio.set_playing(false);
            Ok(())
        }

        /// Start capturing raw microphone PCM independently of accompaniment playback.
        ///
        /// # Errors
        /// Reports stream start failures and unavailable microphones.
        pub fn start_microphone(&self) -> Result<(), String> {
            let stream = self
                .input
                .as_ref()
                .ok_or_else(|| "No microphone stream".to_owned())?;
            stream.play().map_err(|error| error.to_string())
        }

        /// # Errors
        /// Returns a stream pause error if the microphone exists.
        pub fn stop_microphone(&self) -> Result<(), String> {
            let stream = self
                .input
                .as_ref()
                .ok_or_else(|| "No microphone stream".to_owned())?;
            stream.pause().map_err(|error| error.to_string())
        }

        #[must_use]
        pub fn has_microphone(&self) -> bool {
            self.input.is_some()
        }
    }

    impl Drop for AudioDeviceStreams {
        fn drop(&mut self) {
            self.audio.set_playing(false);
        }
    }
}

#[cfg(not(windows))]
mod platform {
    use super::{AudioDeviceInfo, AudioIo, WasapiMode};

    pub(super) fn enumerate_devices() -> Result<Vec<AudioDeviceInfo>, String> {
        Err("The current production CPAL/WASAPI device backend requires Windows".to_owned())
    }

    pub struct AudioDeviceStreams;

    impl AudioDeviceStreams {
        /// # Errors
        /// Native WASAPI is not available on non-Windows platforms.
        pub fn open(
            _output_name: Option<&str>,
            _input_name: Option<&str>,
            _mode: WasapiMode,
            _capacity_frames: usize,
        ) -> Result<Self, String> {
            Err("The current production CPAL/WASAPI device backend requires Windows".to_owned())
        }

        #[must_use]
        pub fn has_microphone(&self) -> bool {
            false
        }

        /// # Errors
        /// Native WASAPI is not available on non-Windows platforms.
        pub fn play(&self) -> Result<(), String> {
            Err("WASAPI unavailable".to_owned())
        }

        /// # Errors
        /// Native WASAPI is not available on non-Windows platforms.
        pub fn pause(&self) -> Result<(), String> {
            Err("WASAPI unavailable".to_owned())
        }

        /// # Errors
        /// Native WASAPI is not available on non-Windows platforms.
        pub fn start_microphone(&self) -> Result<(), String> {
            Err("WASAPI unavailable".to_owned())
        }

        /// # Errors
        /// Native WASAPI is not available on non-Windows platforms.
        pub fn stop_microphone(&self) -> Result<(), String> {
            Err("WASAPI unavailable".to_owned())
        }

        #[must_use]
        pub fn audio(&self) -> AudioIo {
            AudioIo::default()
        }
    }
}

pub use platform::AudioDeviceStreams;
