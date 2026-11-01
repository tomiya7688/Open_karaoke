//! Syntax and size limits for artifact references crossing the Core API.
//!
//! Core owns the public loopback boundary, so it rejects malformed references
//! before a Job is admitted and refuses oversized worker results before they
//! reach Job snapshots or SSE. The worker still resolves paths against its
//! artifact root (existence, symlinks, containment); this module only checks
//! what can be decided without file I/O.

use std::fmt;

/// Maximum characters in one artifact reference (matches the worker contract).
pub const MAX_ARTIFACT_REF_CHARS: usize = 1024;
/// Maximum artifact references one completed analysis Job may expose.
pub const MAX_JOB_ARTIFACTS: usize = 256;

/// {
///   責務: [`ArtifactRefError`: artifact参照を拒否した理由を呼び出し側が判別できる形で表す]
///   フィールド: []
/// }
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ArtifactRefError {
    /// Empty string, or an empty, `.` or `..` path segment.
    NotNormalizedRelative,
    /// Backslash, drive/stream colon, or a control character.
    ForbiddenCharacter,
    /// Longer than [`MAX_ARTIFACT_REF_CHARS`].
    TooLong,
    /// More than [`MAX_JOB_ARTIFACTS`] references in one Job.
    TooMany,
}

impl ArtifactRefError {
    /// {
    ///   責務: [`code`: API・Job errorに載せる安定した機械可読コードを返す]
    ///   処理: [1: 拒否理由をコード文字列へ対応付ける]
    ///   引数: []
    ///   戻り値: [`invalid_artifact_ref` などの固定コード]
    /// }
    #[must_use]
    pub fn code(self) -> &'static str {
        match self {
            Self::NotNormalizedRelative | Self::ForbiddenCharacter => "invalid_artifact_ref",
            Self::TooLong => "artifact_ref_too_long",
            Self::TooMany => "too_many_artifacts",
        }
    }
}

impl fmt::Display for ArtifactRefError {
    /// {
    ///   責務: [`fmt`: 秘密やパス本文を含まない回復用メッセージを書く]
    ///   処理: [1: コードと説明を書き出す]
    ///   引数: [`f`: 出力先]
    ///   戻り値: [書き込み結果]
    /// }
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let detail = match self {
            Self::NotNormalizedRelative => {
                "artifact reference must be a normalized relative path using '/'"
            }
            Self::ForbiddenCharacter => {
                "artifact reference contains a backslash, colon or control character"
            }
            Self::TooLong => "artifact reference exceeds 1024 characters",
            Self::TooMany => "job exposes more than 256 artifact references",
        };
        write!(f, "{}: {detail}", self.code())
    }
}

impl std::error::Error for ArtifactRefError {}

/// {
///   責務: [`validate_ref`: 1件のartifact参照がartifact root相対の正規形か検査する]
///   処理: [1: 文字数上限を確認する, 2: 禁止文字を拒否する, 3: 空・`.`・`..`セグメントを拒否する]
///   引数: [`reference`: クライアントまたはworkerが渡した参照文字列]
///   戻り値: [正規形なら`Ok(())`、そうでなければ拒否理由]
/// }
///
/// # Errors
///
/// Returns [`ArtifactRefError`] naming the first violated rule.
pub fn validate_ref(reference: &str) -> Result<(), ArtifactRefError> {
    if reference.chars().count() > MAX_ARTIFACT_REF_CHARS {
        return Err(ArtifactRefError::TooLong);
    }
    if reference
        .chars()
        .any(|c| c == '\\' || c == ':' || c.is_control())
    {
        return Err(ArtifactRefError::ForbiddenCharacter);
    }
    if reference
        .split('/')
        .any(|segment| matches!(segment, "" | "." | ".."))
    {
        return Err(ArtifactRefError::NotNormalizedRelative);
    }
    Ok(())
}

/// {
///   責務: [`validate_job_artifacts`: worker完了結果のartifact一覧が件数・構文上限内か検査する]
///   処理: [1: 件数上限を確認する, 2: 各参照を`validate_ref`で検査する]
///   引数: [`artifacts`: workerが報告したartifact参照一覧]
///   戻り値: [全件有効なら`Ok(())`、最初に見つけた拒否理由]
/// }
///
/// # Errors
///
/// Returns [`ArtifactRefError::TooMany`] or the first invalid reference's error.
pub fn validate_job_artifacts(artifacts: &[String]) -> Result<(), ArtifactRefError> {
    if artifacts.len() > MAX_JOB_ARTIFACTS {
        return Err(ArtifactRefError::TooMany);
    }
    artifacts
        .iter()
        .try_for_each(|artifact| validate_ref(artifact))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accepts_normalized_relative_references() {
        for ok in ["input.wav", "jobs/abc/stems/vocals.wav", "日本語/曲.wav"] {
            assert_eq!(validate_ref(ok), Ok(()), "{ok}");
        }
        assert_eq!(validate_ref(&"a".repeat(MAX_ARTIFACT_REF_CHARS)), Ok(()));
        // Multibyte references are limited by characters, like the worker.
        assert_eq!(validate_ref(&"曲".repeat(MAX_ARTIFACT_REF_CHARS)), Ok(()));
    }

    #[test]
    fn rejects_traversal_absolute_and_non_normalized_paths() {
        for bad in [
            "",
            "/abs.wav",
            "a//b.wav",
            "a/",
            "./a.wav",
            "a/../b.wav",
            "..",
        ] {
            assert_eq!(
                validate_ref(bad),
                Err(ArtifactRefError::NotNormalizedRelative),
                "{bad:?}"
            );
        }
    }

    #[test]
    fn rejects_windows_paths_streams_urls_and_control_characters() {
        for bad in [
            "C:/x.wav",
            "a\\b.wav",
            "\\\\server\\share\\x.wav",
            "a.wav:stream",
            "https://example.com/a.wav",
            "a\0.wav",
            "a\n.wav",
        ] {
            assert_eq!(
                validate_ref(bad),
                Err(ArtifactRefError::ForbiddenCharacter),
                "{bad:?}"
            );
        }
    }

    #[test]
    fn length_and_count_limits_are_distinguishable() {
        let long = "a".repeat(MAX_ARTIFACT_REF_CHARS + 1);
        assert_eq!(validate_ref(&long), Err(ArtifactRefError::TooLong));
        assert_eq!(
            ArtifactRefError::TooLong.to_string(),
            "artifact_ref_too_long: artifact reference exceeds 1024 characters"
        );

        let at_limit = vec!["jobs/x/a.json".to_owned(); MAX_JOB_ARTIFACTS];
        assert_eq!(validate_job_artifacts(&at_limit), Ok(()));
        let over = vec!["jobs/x/a.json".to_owned(); MAX_JOB_ARTIFACTS + 1];
        assert_eq!(
            validate_job_artifacts(&over),
            Err(ArtifactRefError::TooMany)
        );
        let mixed = vec!["jobs/x/a.json".to_owned(), "../escape".to_owned()];
        assert_eq!(
            validate_job_artifacts(&mixed),
            Err(ArtifactRefError::NotNormalizedRelative)
        );
    }
}
