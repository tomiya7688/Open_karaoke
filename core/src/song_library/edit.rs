//! 公開済み曲への手編集の保存（#38 スライス38.2）。
//!
//! 保存は「完全な新ディレクトリを私的stagingへ組み立て → 検証 → ディレクトリ置換」で原子的に行う。
//! 音声などの大きな資産はハードリンク（不可ならコピー）で共有し、文書のみ書き直す。
//! 失敗時は元の曲ディレクトリを一切変更しない。

#![allow(clippy::doc_markdown)] // JSON風宣言コメントがsnake_caseのフィールド名を含む

use std::{
    fs::{self, File},
    io,
    path::Path,
};

use serde::Serialize;
use uuid::Uuid;

use super::{
    DOCUMENTS, ImportError, MAX_JSON, SongLibrary, Staging,
    files::{check_directory, digest, regular_file, under, write_new_json},
};
use crate::{
    song_data::{ArtifactKind, SongBundle, SongStore},
    song_edit::{EditCommand, EditError, EditOp, apply_edit},
};

/// 1曲ディレクトリ内で複製するエントリ数の上限（異常ディレクトリでの暴走防止）。
const MAX_CLONE_ENTRIES: usize = 1024;
/// 複製するディレクトリ深さの上限。
const MAX_CLONE_DEPTH: usize = 8;

/// {
///   責務: [`edit_generation`: 手編集の世代(競合判定の`base_generation`と比べる値)を返す]
///   処理: [Evidence artifactのgenerationを返す。編集が成功するたびEvidenceは必ず再生成され+1される。無ければ0]
///   引数: [bundle]
///   戻り値: [世代(u64)]
/// }
#[must_use]
pub fn edit_generation(bundle: &SongBundle) -> u64 {
    bundle
        .song
        .artifacts
        .get(&ArtifactKind::Evidence)
        .map_or(0, |metadata| metadata.generation)
}

/// {
///   責務: [`EditedSong`: 保存に成功した編集の結果]
///   フィールド: [song_id, generation(保存後の編集世代。次の編集のbase_generation), pending(再生成待ち=valid=falseのartifact)]
/// }
#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
pub struct EditedSong {
    pub song_id: Uuid,
    pub generation: u64,
    pub pending: Vec<ArtifactKind>,
}

/// {
///   責務: [`SongEditSaveError`: 編集保存の失敗分類。いずれも元の曲を変更していない]
///   バリアント: [Edit(編集内容の拒否), GenerationConflict(409相当。expected=コマンドの値, current=保存済みの値), Library(保存層のエラー: NotFound/Busy/TooLarge/Io等)]
/// }
#[derive(Debug)]
pub enum SongEditSaveError {
    Edit(EditError),
    GenerationConflict { expected: u64, current: u64 },
    Library(ImportError),
}

impl std::fmt::Display for SongEditSaveError {
    fn fmt(&self, out: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Edit(error) => write!(out, "song_edit_rejected: {error}"),
            Self::GenerationConflict { expected, current } => write!(
                out,
                "song_edit_conflict: base_generation {expected} is stale, current is {current}"
            ),
            Self::Library(error) => write!(out, "{error}"),
        }
    }
}
impl std::error::Error for SongEditSaveError {}
impl From<EditError> for SongEditSaveError {
    fn from(error: EditError) -> Self {
        Self::Edit(error)
    }
}
impl From<ImportError> for SongEditSaveError {
    fn from(error: ImportError) -> Self {
        Self::Library(error)
    }
}
impl From<io::Error> for SongEditSaveError {
    fn from(error: io::Error) -> Self {
        Self::Library(error.into())
    }
}
impl From<serde_json::Error> for SongEditSaveError {
    fn from(error: serde_json::Error) -> Self {
        Self::Library(error.into())
    }
}

impl SongLibrary {
    /// {
    ///   責務: [`edit_song`: 公開済み曲へ編集を原子的に保存し、世代を進め、下流artifactをinvalidateする]
    ///   処理: [ライブラリのwriterロック取得(競合はBusy) → 中断された置換の復旧 → 読込 → base_generation照合 → apply_edit(検証) → 下流invalidate/再生成 → サイズ上限確認 → staging組立(資産はハードリンク) → 文書書込+fsync+領収書更新 → 往復検証 → ディレクトリ置換]
    ///   引数: [id(曲ID), command(song_idがidと一致すること)]
    ///   戻り値: [Ok(EditedSong) / Err(SongEditSaveError)。失敗時は元の曲ディレクトリ不変]
    ///   下流規則: [歌詞opあり→Lyricsをinvalidate(Notes/Evidenceへ伝播)後にLyricsを再生成。ノートopあり→Notesも同様。Evidenceは毎回再生成(=世代+1)。再生成されなかった派生artifactはvalid=false(再生成待ち)のまま公開され、一覧/再生から消えない]
    /// }
    ///
    /// # Errors
    /// 曲が無い・世代不一致・編集違反・文書サイズ超過・書込失敗・他の書込中(Busy)で`SongEditSaveError`を返す。
    pub fn edit_song(
        &self,
        id: Uuid,
        command: &EditCommand,
    ) -> Result<EditedSong, SongEditSaveError> {
        check_directory(&self.root)?;
        let _lock = self.writer_lock()?;
        self.recover_retired(id)?;
        let final_path = under(&self.root, &id.to_string())?;
        if !final_path.try_exists().map_err(ImportError::from)? {
            return Err(ImportError::NotFound.into());
        }
        let (bundle, mut receipt) = Self::load_at(&final_path, id)?;
        let current = edit_generation(&bundle);
        if command.base_generation != current {
            return Err(SongEditSaveError::GenerationConflict {
                expected: command.base_generation,
                current,
            });
        }
        let mut next = apply_edit(&bundle, command)?;
        propagate_downstream(&mut next, &command.ops);
        check_document_sizes(&next)?;

        let staging = Staging::new(&self.root)?;
        clone_tree(&final_path, &staging.path, 0, &mut 0)?;
        SongStore::save(&staging.path, &next)
            .map_err(|_| ImportError::Invalid("SongStore serialization failed"))?;
        for document in DOCUMENTS {
            File::options()
                .write(true)
                .open(under(&staging.path, document)?)
                .map_err(ImportError::from)?
                .sync_all()
                .map_err(ImportError::from)?;
        }
        receipt.bundle_sha256 = digest(&serde_json::to_vec(&next)?);
        write_new_json(&staging.path.join("import-receipt.json"), &receipt)?;
        if Self::load_at(&staging.path, id)?.0 != next {
            return Err(ImportError::Invalid("edited metadata did not round-trip").into());
        }

        let retired = self.root.join(format!(".retired-{id}-{}", Uuid::new_v4()));
        fs::rename(&final_path, &retired).map_err(ImportError::from)?;
        if let Err(error) = fs::rename(&staging.path, &final_path) {
            // 置換に失敗したら元へ戻す。戻せなければ.retired-*が次回の復旧対象として残る。
            if fs::rename(&retired, &final_path).is_err() {
                // 元の曲は.retired-*に無傷で残り、次回のedit_song/importがwriterロック下で復旧する。
                return Err(ImportError::Invalid(
                    "song_edit_swap_failed: original kept as .retired-*, recovered on next write",
                )
                .into());
            }
            return Err(ImportError::from(error).into());
        }
        if let Err(error) = fs::remove_dir_all(&retired)
            && error.kind() != io::ErrorKind::NotFound
        {
            eprintln!("song_edit_cleanup_failed: retired directory may remain");
        }
        Ok(EditedSong {
            song_id: id,
            generation: edit_generation(&next),
            pending: next
                .song
                .artifacts
                .iter()
                .filter(|(_, metadata)| !metadata.valid)
                .map(|(kind, _)| *kind)
                .collect(),
        })
    }

    /// {
    ///   責務: [`recover_retired`: 置換の途中で中断された曲を復旧する。writerロック保持中に呼ぶ]
    ///   処理: [`.retired-<id>-*`を走査。公開ディレクトリが無ければ戻す(元の曲を保つ)。有れば置換成功後の残骸として削除]
    ///   引数: [id]
    ///   戻り値: [Ok / ImportError]
    /// }
    pub(super) fn recover_retired(&self, id: Uuid) -> Result<(), ImportError> {
        let prefix = format!(".retired-{id}-");
        let live = under(&self.root, &id.to_string())?;
        for entry in fs::read_dir(&*self.root)? {
            let entry = entry?;
            if !entry.file_name().to_string_lossy().starts_with(&prefix) {
                continue;
            }
            if live.try_exists()? {
                if let Err(error) = fs::remove_dir_all(entry.path())
                    && error.kind() != io::ErrorKind::NotFound
                {
                    eprintln!("song_edit_cleanup_failed: retired directory may remain");
                }
            } else {
                fs::rename(entry.path(), &live)?;
            }
        }
        Ok(())
    }
}

/// {
///   責務: [`propagate_downstream`: 編集したartifactと下流の世代・valid状態を更新する]
///   引数: [next(編集後の束), ops(適用したop)]
///   戻り値: [なし。歌詞op→Lyrics、ノートop→Notesをinvalidateして再生成、Evidenceは常に再生成]
/// }
fn propagate_downstream(next: &mut SongBundle, ops: &[EditOp]) {
    let lyrics = ops.iter().any(|op| {
        matches!(
            op,
            EditOp::UpdateLyric { .. } | EditOp::AddLyric { .. } | EditOp::DeleteLyric { .. }
        )
    });
    let notes = ops.iter().any(|op| {
        matches!(
            op,
            EditOp::UpdateNote { .. } | EditOp::AddNote { .. } | EditOp::DeleteNote { .. }
        )
    });
    let version = next.song.analysis_version;
    if lyrics {
        next.invalidate(ArtifactKind::Lyrics);
        next.mark_regenerated(ArtifactKind::Lyrics, version);
    }
    if notes {
        next.invalidate(ArtifactKind::Notes);
        next.mark_regenerated(ArtifactKind::Notes, version);
    }
    next.mark_regenerated(ArtifactKind::Evidence, version);
}

/// {
///   責務: [`check_document_sizes`: 書き出す各文書(整形JSON)が読込側上限`MAX_JSON`以内か確認]
///   引数: [bundle]
///   戻り値: [Ok / ImportError::TooLarge(保存前に拒否し、読めない曲を作らない)]
/// }
fn check_document_sizes(bundle: &SongBundle) -> Result<(), ImportError> {
    let limit = usize::try_from(MAX_JSON).unwrap_or(usize::MAX);
    let sizes = [
        serde_json::to_vec_pretty(&bundle.song)?.len(),
        serde_json::to_vec_pretty(&bundle.lyrics)?.len(),
        serde_json::to_vec_pretty(&bundle.notes)?.len(),
        serde_json::to_vec_pretty(&bundle.evidence)?.len(),
    ];
    if sizes.iter().any(|size| *size >= limit) {
        return Err(ImportError::TooLarge);
    }
    Ok(())
}

/// {
///   責務: [`clone_tree`: 曲ディレクトリの資産をstagingへ複製する。書き直す文書(song.json/analysis/import-receipt.json)は除く]
///   処理: [通常ファイル/ディレクトリのみ(リンク・特殊ファイルは拒否) → ハードリンク、不可ならコピー。件数・深さ上限]
///   引数: [source, destination, depth(0=曲ルート), count(複製数)]
///   戻り値: [Ok / ImportError]
/// }
fn clone_tree(
    source: &Path,
    destination: &Path,
    depth: usize,
    count: &mut usize,
) -> Result<(), ImportError> {
    if depth > MAX_CLONE_DEPTH {
        return Err(ImportError::Invalid("song directory is nested too deeply"));
    }
    check_directory(source)?;
    for entry in fs::read_dir(source)? {
        let entry = entry?;
        let name = entry.file_name();
        if depth == 0
            && matches!(
                name.to_str(),
                Some("song.json" | "analysis" | "import-receipt.json")
            )
        {
            continue;
        }
        *count += 1;
        if *count > MAX_CLONE_ENTRIES {
            return Err(ImportError::TooLarge);
        }
        let from = entry.path();
        let to = destination.join(&name);
        if fs::symlink_metadata(&from)?.is_dir() {
            check_directory(&from)?;
            fs::create_dir(&to)?;
            clone_tree(&from, &to, depth + 1, count)?;
        } else {
            regular_file(&from)?;
            if fs::hard_link(&from, &to).is_err() {
                fs::copy(&from, &to)?;
            }
        }
    }
    Ok(())
}
