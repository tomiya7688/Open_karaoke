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
///   フィールド: [source: 出所, `selected_model_id`: 保存済み選択のID, `fallback_reason`: fallback理由]
/// }
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct StagePlan {
    pub source: ModelSource,
    #[serde(default)]
    pub selected_model_id: Option<String>,
    #[serde(default)]
    pub fallback_reason: Option<String>,
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
}
