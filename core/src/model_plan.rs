//! Model-plan provenance carried from the analysis worker into the public Job snapshot.
//!
//! The worker pins which model each stage uses when a Job is accepted. Core treats that
//! data as untrusted input: it is parsed into closed types, size-limited and rejected as
//! `analysis_protocol` on any violation, and it never contains absolute paths or secrets.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

use crate::Job;

/// Maximum characters of one model id or fallback reason.
pub const MAX_TEXT_CHARS: usize = 512;
/// Maximum stages in one plan (song has six).
pub const MAX_PLAN_STAGES: usize = 16;
/// Maximum characters of a stage name.
pub const MAX_STAGE_NAME_CHARS: usize = 32;
/// Maximum runtime package entries recorded for one stage.
pub const MAX_RUNTIME_ENTRIES: usize = 32;
/// Maximum characters of one runtime package name, version or device name.
pub const MAX_RUNTIME_TEXT_CHARS: usize = 64;

/// {
///   責務: [`ModelSource`: モデルが決まった出所。選択したものと実際に動いたものの区別に使う]
///   フィールド: []
/// }
#[derive(Clone, Copy, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ModelSource {
    /// Explicit `options.models` entry.
    Request,
    /// Valid saved selection.
    Selection,
    /// Official default (nothing selected).
    Default,
    /// Saved selection was unusable, the default ran instead.
    DefaultFallback,
}

/// {
///   責務: [`StagePlan`: 1段の確定プラン(出所・保存済み選択ID・fallback理由)]
///   フィールド: [
///     source: 出所, `selected_model_id`: 保存済み選択のID, `fallback_reason`: fallback理由,
///     `weights_sha256`: 実際に使った重みのSHA-256(完了後。公開しないAdapterは無し),
///     `runtime_versions`: 実行時パッケージ版(完了後), device: 実行デバイス(完了後)
///   ]
/// }
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct StagePlan {
    pub source: ModelSource,
    #[serde(default)]
    pub selected_model_id: Option<String>,
    #[serde(default)]
    pub fallback_reason: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub weights_sha256: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub runtime_versions: Option<BTreeMap<String, Option<String>>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub device: Option<String>,
}

/// {
///   責務: [`ModelPlan`: Job受付時に確定した段別プランと解決時刻。stage名 -> `StagePlan` の平坦な形]
///   フィールド: [`resolved_at`: 解決時刻(`ISO8601` UTC文字列), stages: 段名ごとのプラン]
/// }
#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq, Eq)]
pub struct ModelPlan {
    #[serde(default)]
    pub resolved_at: Option<String>,
    #[serde(flatten)]
    pub stages: BTreeMap<String, StagePlan>,
}

/// {
///   責務: [`ModelProvenance`: Job応答へ転送するモデルの出所一式(worker入力。公開前にvalidateする)]
///   フィールド: [`model_id`: 実際に動いたAdapter, `model_source`/`selected_model_id`/`selection_fallback_reason`: 選択の決まり方, `model_plan`: 段別プラン]
/// }
#[derive(Clone, Debug, Default, Deserialize, PartialEq, Eq)]
pub(crate) struct ModelProvenance {
    #[serde(default)]
    pub model_id: Option<String>,
    #[serde(default)]
    pub model_source: Option<ModelSource>,
    #[serde(default)]
    pub selected_model_id: Option<String>,
    #[serde(default)]
    pub selection_fallback_reason: Option<String>,
    #[serde(default)]
    pub model_plan: Option<ModelPlan>,
}

fn short_text_ok(text: &str) -> bool {
    !text.is_empty()
        && text.chars().count() <= MAX_RUNTIME_TEXT_CHARS
        && !text.chars().any(char::is_control)
}

fn digest_ok(digest: &str) -> bool {
    digest.len() == 64
        && digest
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

impl StagePlan {
    /// {
    ///   責務: [`runtime_ok`: 完了後に足される重みdigestと実行環境の形を検査する]
    ///   処理: [1: digestは64桁の小文字16進数, 2: 版情報は件数・文字数の上限と制御文字なし, 3: deviceは短い文字列]
    ///   引数: []
    ///   戻り値: [正しければtrue]
    /// }
    fn runtime_ok(&self) -> bool {
        self.weights_sha256.as_deref().is_none_or(digest_ok)
            && self.device.as_deref().is_none_or(short_text_ok)
            && self.runtime_versions.as_ref().is_none_or(|versions| {
                versions.len() <= MAX_RUNTIME_ENTRIES
                    && versions
                        .iter()
                        .all(|(k, v)| short_text_ok(k) && v.as_deref().is_none_or(short_text_ok))
            })
    }
}

fn text_ok(value: Option<&String>) -> bool {
    value.is_none_or(|text| {
        text.chars().count() <= MAX_TEXT_CHARS && !text.chars().any(char::is_control)
    })
}

impl ModelPlan {
    /// {
    ///   責務: [`validate`: 段数・名前・文字列の長さと制御文字を検査する]
    ///   処理: [1: 段数上限, 2: 段名の形, 3: 各文字列の長さ・制御文字, 4: fallbackには理由が必要]
    ///   引数: []
    ///   戻り値: [違反なしでOk、あれば固定の理由文字列]
    /// }
    ///
    /// # Errors
    /// Returns a short reason when the plan is oversized or malformed.
    pub fn validate(&self) -> Result<(), &'static str> {
        if self.stages.len() > MAX_PLAN_STAGES || !text_ok(self.resolved_at.as_ref()) {
            return Err("model_plan is oversized");
        }
        for (name, stage) in &self.stages {
            let name_ok = !name.is_empty()
                && name.chars().count() <= MAX_STAGE_NAME_CHARS
                && name
                    .chars()
                    .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-');
            if !name_ok
                || !text_ok(stage.selected_model_id.as_ref())
                || !text_ok(stage.fallback_reason.as_ref())
                || !stage.runtime_ok()
            {
                return Err("model_plan stage is malformed");
            }
            if stage.source == ModelSource::DefaultFallback && stage.fallback_reason.is_none() {
                return Err("model_plan fallback has no reason");
            }
        }
        Ok(())
    }
}

impl ModelProvenance {
    /// {
    ///   責務: [`validate`: worker由来のprovenance全体を公開前に検査する]
    ///   処理: [1: 文字列の長さ・制御文字, 2: `default_fallback`には理由が必要, 3: プラン検査]
    ///   引数: []
    ///   戻り値: [違反なしでOk、あれば固定の理由文字列]
    /// }
    ///
    /// # Errors
    /// Returns a short reason when the `provenance` must not be published.
    pub(crate) fn validate(&self) -> Result<(), &'static str> {
        if !text_ok(self.model_id.as_ref())
            || !text_ok(self.selected_model_id.as_ref())
            || !text_ok(self.selection_fallback_reason.as_ref())
        {
            return Err("model provenance text is invalid");
        }
        if self.model_source == Some(ModelSource::DefaultFallback)
            && self.selection_fallback_reason.is_none()
        {
            return Err("model fallback has no reason");
        }
        self.model_plan.as_ref().map_or(Ok(()), ModelPlan::validate)
    }
}

impl Job {
    /// {
    ///   責務: [`require_exact_model_plan`: 比較評価(Evaluator比較モード)用のfail closed判定。選択どおりに動いたと言えないJobを拒否する]
    ///   処理: [1: `provenance`が無ければ拒否, 2: `default_fallback`が1つでもあれば拒否, 3: `analysis.song`はstage別プラン必須]
    ///   引数: []
    ///   戻り値: [比較に使えるときOk、使えないとき理由コード付き文字列]
    /// }
    ///
    /// # Errors
    /// Returns `model_plan_missing` or `model_plan_fallback` when the Job cannot be compared.
    pub fn require_exact_model_plan(&self) -> Result<(), String> {
        let Some(source) = self.model_source else {
            return Err("model_plan_missing: job has no model provenance".to_owned());
        };
        if source == ModelSource::DefaultFallback {
            return Err(
                "model_plan_fallback: a saved selection fell back to the default".to_owned(),
            );
        }
        if self.kind == "analysis.song" {
            let Some(plan) = self.model_plan.as_ref().filter(|p| !p.stages.is_empty()) else {
                return Err("model_plan_missing: song job has no stage plan".to_owned());
            };
            if let Some((stage, _)) = plan
                .stages
                .iter()
                .find(|(_, s)| s.source == ModelSource::DefaultFallback)
            {
                return Err(format!(
                    "model_plan_fallback: stage {stage} fell back to the default"
                ));
            }
        }
        Ok(())
    }

    /// {
    ///   責務: [`require_matching_model_digests`: 比較評価で、基準Jobと同じ重みdigest・実行環境で動いたことを確認するfail closed判定]
    ///   処理: [
    ///     1: 両方が`require_exact_model_plan`を満たすこと,
    ///     2: 段の集合が一致すること,
    ///     3: 各段の`weights_sha256`・`runtime_versions`・deviceが基準と完全一致(片方だけ無い場合も不一致)
    ///   ]
    ///   引数: [baseline: 比較の基準になるJob]
    ///   戻り値: [同一と言えるときOk、言えないとき理由コード付き文字列]
    /// }
    ///
    /// # Errors
    /// Returns `model_plan_missing`, `model_plan_fallback`, `model_digest_mismatch` or
    /// `model_runtime_mismatch` when the two Jobs cannot be compared as like for like.
    pub fn require_matching_model_digests(&self, baseline: &Job) -> Result<(), String> {
        self.require_exact_model_plan()?;
        baseline.require_exact_model_plan()?;
        let empty = ModelPlan::default();
        let ours = self.model_plan.as_ref().unwrap_or(&empty);
        let theirs = baseline.model_plan.as_ref().unwrap_or(&empty);
        if ours.stages.keys().ne(theirs.stages.keys()) {
            return Err("model_plan_missing: the jobs have different stage sets".to_owned());
        }
        for (stage, expected) in &theirs.stages {
            let actual = &ours.stages[stage];
            if actual.weights_sha256 != expected.weights_sha256 {
                return Err(format!(
                    "model_digest_mismatch: stage {stage} used different weights"
                ));
            }
            if actual.runtime_versions != expected.runtime_versions
                || actual.device != expected.device
            {
                return Err(format!(
                    "model_runtime_mismatch: stage {stage} ran in a different runtime"
                ));
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn provenance(value: serde_json::Value) -> ModelProvenance {
        serde_json::from_value(value).unwrap()
    }

    fn song_job(plan: serde_json::Value, source: &str) -> Job {
        let mut job: Job = serde_json::from_value(json!({
            "id": uuid::Uuid::new_v4(), "kind": "analysis.song", "status": "completed",
            "progress": 1.0, "stage": "completed",
            "created_at": "2026-01-01T00:00:00Z", "started_at": null, "finished_at": null,
            "error": null, "artifacts": [], "model_source": source,
        }))
        .unwrap();
        job.model_plan = Some(serde_json::from_value(plan).unwrap());
        job
    }

    #[test]
    fn worker_plan_round_trips_in_the_flat_wire_shape() {
        let wire = json!({
            "resolved_at": "2026-01-01T00:00:00+00:00",
            "lyrics": {"source": "selection", "selected_model_id": "alt", "fallback_reason": null},
            "pitch": {"source": "default", "selected_model_id": null, "fallback_reason": null},
        });
        let parsed = provenance(json!({"model_id": "m", "model_plan": wire}));
        assert_eq!(parsed.validate(), Ok(()));
        let plan = parsed.model_plan.unwrap();
        assert_eq!(plan.stages["lyrics"].source, ModelSource::Selection);
        assert_eq!(serde_json::to_value(&plan).unwrap(), wire);
    }

    #[test]
    fn malformed_or_oversized_provenance_is_rejected() {
        assert!(
            serde_json::from_value::<ModelProvenance>(json!({"model_source": "bogus"})).is_err()
        );
        let unknown = json!({"model_plan": {"stems": {"source": "default", "extra": 1}}});
        assert!(serde_json::from_value::<ModelProvenance>(unknown).is_err());
        let long = "x".repeat(MAX_TEXT_CHARS + 1);
        assert!(provenance(json!({"model_id": long})).validate().is_err());
        assert!(
            provenance(json!({"selected_model_id": "a\nb"}))
                .validate()
                .is_err()
        );
        let bad_stage = json!({"model_plan": {"../x": {"source": "default"}}});
        assert!(provenance(bad_stage).validate().is_err());
        let many: serde_json::Map<_, _> = (0..=MAX_PLAN_STAGES)
            .map(|i| (format!("s{i}"), json!({"source": "default"})))
            .collect();
        assert!(provenance(json!({"model_plan": many})).validate().is_err());
        let no_reason = json!({"model_source": "default_fallback"});
        assert!(provenance(no_reason).validate().is_err());
    }

    #[test]
    fn comparison_mode_fails_closed_on_fallback_or_missing_plan() {
        let exact = json!({"resolved_at": "t", "lyrics": {"source": "selection", "selected_model_id": "a"}});
        assert_eq!(
            song_job(exact, "selection").require_exact_model_plan(),
            Ok(())
        );
        let stage_fallback = json!({"pitch": {"source": "default_fallback", "selected_model_id": "g", "fallback_reason": "gone"}});
        let error = song_job(stage_fallback.clone(), "default")
            .require_exact_model_plan()
            .unwrap_err();
        assert!(error.starts_with("model_plan_fallback:"), "{error}");
        let error = song_job(json!({}), "default")
            .require_exact_model_plan()
            .unwrap_err();
        assert!(error.starts_with("model_plan_missing:"), "{error}");
        let error = song_job(stage_fallback, "default_fallback")
            .require_exact_model_plan()
            .unwrap_err();
        assert!(error.starts_with("model_plan_fallback:"), "{error}");
        let mut no_source = song_job(json!({"a": {"source": "default"}}), "default");
        no_source.model_source = None;
        assert!(no_source.require_exact_model_plan().is_err());
    }

    fn with_runtime(digest: &str, version: &str, device: &str) -> Job {
        song_job(
            json!({"stems": {
                "source": "default", "weights_sha256": digest, "device": device,
                "runtime_versions": {"torch": version},
            }, "pitch": {"source": "default"}}),
            "default",
        )
    }

    #[test]
    fn runtime_fields_round_trip_and_are_validated() {
        let digest = "ab".repeat(32);
        let wire = json!({"resolved_at": null, "stems": {
            "source": "default", "selected_model_id": null, "fallback_reason": null,
            "weights_sha256": digest, "device": "cpu",
            "runtime_versions": {"torch": "2.8.0", "numpy": null},
        }});
        let parsed = provenance(json!({"model_plan": wire}));
        assert_eq!(parsed.validate(), Ok(()));
        let plan = parsed.model_plan.unwrap();
        assert_eq!(serde_json::to_value(&plan).unwrap(), wire);
        for bad in [
            json!({"weights_sha256": "AB".repeat(32)}),
            json!({"weights_sha256": "ab"}),
            json!({"device": "c\npu"}),
            json!({"device": "x".repeat(MAX_RUNTIME_TEXT_CHARS + 1)}),
            json!({"runtime_versions": {"": "1"}}),
            json!({"runtime_versions": {"a": "x".repeat(MAX_RUNTIME_TEXT_CHARS + 1)}}),
        ] {
            let mut stage = json!({"source": "default"});
            stage
                .as_object_mut()
                .unwrap()
                .extend(bad.as_object().unwrap().clone());
            assert!(
                provenance(json!({"model_plan": {"s": stage}}))
                    .validate()
                    .is_err()
            );
        }
        let many: serde_json::Map<_, _> = (0..=MAX_RUNTIME_ENTRIES)
            .map(|i| (format!("p{i}"), json!("1")))
            .collect();
        let stage = json!({"source": "default", "runtime_versions": many});
        assert!(
            provenance(json!({"model_plan": {"s": stage}}))
                .validate()
                .is_err()
        );
    }

    #[test]
    fn comparison_fails_closed_on_digest_or_runtime_mismatch() {
        let a = "ab".repeat(32);
        let b = "cd".repeat(32);
        let baseline = with_runtime(&a, "2.8.0", "cpu");
        assert_eq!(
            with_runtime(&a, "2.8.0", "cpu").require_matching_model_digests(&baseline),
            Ok(())
        );
        let error = with_runtime(&b, "2.8.0", "cpu")
            .require_matching_model_digests(&baseline)
            .unwrap_err();
        assert!(error.starts_with("model_digest_mismatch:"), "{error}");
        for other in [
            with_runtime(&a, "2.9.0", "cpu"),
            with_runtime(&a, "2.8.0", "cuda"),
        ] {
            let error = other.require_matching_model_digests(&baseline).unwrap_err();
            assert!(error.starts_with("model_runtime_mismatch:"), "{error}");
        }
        // digestが片方にしか無い、段の集合が違う、fallbackがある場合も拒否する
        let unpublished = song_job(
            json!({"stems": {"source": "default"}, "pitch": {"source": "default"}}),
            "default",
        );
        assert!(
            unpublished
                .require_matching_model_digests(&baseline)
                .is_err()
        );
        let fewer = song_job(json!({"stems": {"source": "default"}}), "default");
        let error = fewer
            .require_matching_model_digests(&unpublished)
            .unwrap_err();
        assert!(error.starts_with("model_plan_missing:"), "{error}");
        let fallback = song_job(
            json!({"pitch": {"source": "default_fallback", "fallback_reason": "gone"}}),
            "default",
        );
        let error = baseline
            .require_matching_model_digests(&fallback)
            .unwrap_err();
        assert!(error.starts_with("model_plan_fallback:"), "{error}");
    }
}
