//! Synthetic exports for storage/API tests, not a singing-accuracy dataset.

use std::{fs, path::Path};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use uuid::Uuid;

fn wave() -> Vec<u8> {
    let mut bytes = Vec::new();
    bytes.extend_from_slice(b"RIFF");
    bytes.extend_from_slice(&1060_u32.to_le_bytes());
    bytes.extend_from_slice(b"WAVEfmt ");
    bytes.extend_from_slice(&16_u32.to_le_bytes());
    bytes.extend_from_slice(&3_u16.to_le_bytes());
    bytes.extend_from_slice(&2_u16.to_le_bytes());
    bytes.extend_from_slice(&48_000_u32.to_le_bytes());
    bytes.extend_from_slice(&384_000_u32.to_le_bytes());
    bytes.extend_from_slice(&8_u16.to_le_bytes());
    bytes.extend_from_slice(&32_u16.to_le_bytes());
    bytes.extend_from_slice(b"data");
    bytes.extend_from_slice(&1024_u32.to_le_bytes());
    for _ in 0..128 {
        bytes.extend_from_slice(&0.25_f32.to_le_bytes());
        bytes.extend_from_slice(&(-0.25_f32).to_le_bytes());
    }
    bytes
}

fn bundle(id: Uuid, hash: &str) -> Value {
    let mut artifacts = serde_json::Map::new();
    for (kind, dependencies) in [
        ("original", vec![]), ("vocals", vec!["original"]),
        ("accompaniment", vec!["original"]), ("lyrics", vec!["vocals"]),
        ("notes", vec!["vocals", "lyrics"]), ("evidence", vec!["lyrics", "notes"]),
    ] {
        artifacts.insert(kind.to_owned(), json!({
            "generation":1,"valid":true,"depends_on":dependencies,"analysis_version":1,
        }));
    }
    let stages = ["stems", "lyrics", "alignment", "pitch", "events", "notes"].map(|role| json!({
        "role":role,"model":{"id":format!("fixture-{role}"),"version":"test-only","role":role},
        "options":{},"artifacts":[],
    }));
    json!({
        "song": {
            "format_version":1,"analysis_version":1,"song_id":id,
            "title":"合成・登録テスト","artist":null,"sample_rate":48000,"duration_samples":128,
            "files": {
                "original":"audio/original_48k.wav","vocals":"audio/vocals.wav",
                "accompaniment":"audio/accompaniment.wav","lyrics":"analysis/lyrics.json",
                "notes":"analysis/notes.json","evidence":"analysis/evidence.json",
            },
            "artifacts":artifacts,
        },
        "lyrics":{"format_version":1,"analysis_version":1,"segments":[{
            "id":"line-1","text":"あ","start_sample":0,"end_sample":128,
            "text_confidence":null,"timing_confidence":null,"source":"fixture",
        }]},
        "notes":{"format_version":1,"analysis_version":1,"notes":[{
            "id":"note-1","start_sample":0,"end_sample":128,"midi_note":69,
            "pitch_confidence":null,"lyric_segment_id":"line-1",
        }]},
        "evidence":{"format_version":1,"analysis_version":1,"items":[{
            "target_id":"song","evidence":{
                "source_sha256":hash,"vocals_sha256":hash,"stages":stages,
                "pipeline_version":1,"singing_accuracy_verified":false,
            },
        }]},
    })
}

pub fn build(root: &Path) -> (Value, Value) {
    let id = Uuid::new_v4();
    let prefix = format!("jobs/{}/{id}", Uuid::new_v4());
    let pcm = wave();
    let hash = format!("{:x}", Sha256::digest(&pcm));
    let bundle = bundle(id, &hash);
    let metadata = json!({"format_version":1,"analysis_version":1,"input_sha256":hash,"sample_rate":48000,"duration_samples":128});
    let mut files = vec![
        ("source/original_48k.wav", pcm.clone()),
        ("stages/stems/vocals.wav", pcm.clone()),
        ("stages/stems/accompaniment.wav", pcm),
        ("source/reference.txt", "あ\n参考歌詞".as_bytes().to_vec()),
    ];
    for (name, document) in [
        ("stages/stems/stems.json", &metadata),
        ("stages/lyrics/asr_raw.json", &metadata), ("stages/lyrics/lyrics.json", &bundle["lyrics"]),
        ("stages/alignment/alignment.json", &metadata),
        ("stages/alignment/alignment_evidence.json", &metadata),
        ("stages/alignment/lyrics.json", &bundle["lyrics"]),
        ("stages/pitch/pitch.json", &metadata), ("stages/events/vocal_events.json", &metadata),
        ("stages/notes/note_evidence.json", &metadata), ("stages/notes/notes.json", &bundle["notes"]),
    ] { files.push((name, serde_json::to_vec(document).unwrap())); }
    let mut assets = Vec::new();
    let mut published = Vec::new();
    for (suffix, data) in files {
        let source = format!("{prefix}/{suffix}");
        let path = root.join(&source);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, &data).unwrap();
        assets.push(json!({
            "source":source,"destination":format!("provenance/{suffix}"),
            "sha256":format!("{:x}", Sha256::digest(&data)),"bytes":data.len(),
        }));
        published.push(source);
    }
    for (source, destination) in [
        ("source/original_48k.wav", "audio/original_48k.wav"),
        ("stages/stems/vocals.wav", "audio/vocals.wav"),
        ("stages/stems/accompaniment.wav", "audio/accompaniment.wav"),
    ] {
        let mut asset = assets.iter().find(|asset| asset["source"] == format!("{prefix}/{source}")).unwrap().clone();
        asset["destination"] = destination.into();
        assets.push(asset);
    }
    published.push(format!("{prefix}/song-result.json"));
    let job = json!({
        "id":Uuid::new_v4(),"kind":"analysis.song","status":"completed","progress":1.0,"stage":"completed",
        "created_at":"2026-10-18T00:00:00Z","started_at":"2026-10-18T00:00:00Z",
        "finished_at":"2026-10-18T00:00:00Z","error":null,"artifacts":published,
    });
    let export = json!({"format_version":1,"bundle":bundle,"assets":assets});
    write_result(root, &job, &export);
    (job, export)
}

pub fn write_result(root: &Path, job: &Value, export: &Value) {
    let result = job["artifacts"].as_array().unwrap().last().unwrap().as_str().unwrap();
    fs::write(root.join(result), serde_json::to_vec(export).unwrap()).unwrap();
}
