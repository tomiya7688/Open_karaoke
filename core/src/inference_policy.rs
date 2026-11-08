//! 重い推論の資源方針の設定面(Issue #43 43.3)。
//!
//! 既定は現行挙動(優先度は継承・同時数は無制限)のまま。実機比較(43.5/H)で効果が
//! 確認されるまで既定を変えない。audio callbackからは参照しない。

use std::num::NonZeroUsize;

/// Windowsの `BELOW_NORMAL_PRIORITY_CLASS`。
#[cfg(windows)]
const BELOW_NORMAL_PRIORITY_CLASS: u32 = 0x0000_4000;
/// Windowsの `IDLE_PRIORITY_CLASS`。
#[cfg(windows)]
const IDLE_PRIORITY_CLASS: u32 = 0x0000_0040;

/// {
///   責務: [`WorkerPriority`: Python worker子プロセスのOS優先度方針]
///   フィールド: [`Inherit`: 親の優先度を継承(既定), `BelowNormal`: 通常より低い, `Idle`: 最低]
/// }
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum WorkerPriority {
    #[default]
    Inherit,
    BelowNormal,
    Idle,
}

impl WorkerPriority {
    /// {
    ///   責務: [`parse`: 設定文字列を優先度方針へ変換する。範囲外は暗黙に丸めず拒否する]
    ///   処理: [1: 前後の空白を除き小文字化する, 2: `inherit` `below_normal` `idle` のいずれかに一致させる, 3: 他はエラー]
    ///   引数: [text: 設定値]
    ///   戻り値: [`Result<Self, String>`: 不正値は理由付きのエラー]
    /// }
    ///
    /// # Errors
    /// 取り得る値以外のとき、受理できる値を示すエラーを返す。
    pub fn parse(text: &str) -> Result<Self, String> {
        match text.trim().to_ascii_lowercase().as_str() {
            "inherit" => Ok(Self::Inherit),
            "below_normal" => Ok(Self::BelowNormal),
            "idle" => Ok(Self::Idle),
            other => Err(format!(
                "worker_priority must be inherit, below_normal or idle (got {other:?})"
            )),
        }
    }

    /// {
    ///   責務: [`effective`: 実行環境で有効な優先度を返す。Windows以外はinheritのみ有効]
    ///   処理: [1: Windowsならそのまま返す, 2: それ以外でinherit以外ならinheritに落として警告文を返す]
    ///   引数: []
    ///   戻り値: [`(Self, Option<String>)`: 有効な方針と、落とした場合の警告文]
    /// }
    #[must_use]
    pub fn effective(self) -> (Self, Option<String>) {
        if cfg!(windows) || self == Self::Inherit {
            return (self, None);
        }
        (
            Self::Inherit,
            Some(format!(
                "worker_priority {self:?} is only supported on Windows; using inherit"
            )),
        )
    }

    /// {
    ///   責務: [`creation_flags`: 子プロセス起動時にWindowsへ渡すpriority classフラグを返す]
    ///   処理: [1: Windowsなら方針に対応するフラグを返す, 2: inheritと非Windowsは0]
    ///   引数: []
    ///   戻り値: [`u32`: inheritまたは非Windowsは0]
    /// }
    #[must_use]
    pub fn creation_flags(self) -> u32 {
        #[cfg(windows)]
        {
            match self {
                Self::Inherit => 0,
                Self::BelowNormal => BELOW_NORMAL_PRIORITY_CLASS,
                Self::Idle => IDLE_PRIORITY_CLASS,
            }
        }
        #[cfg(not(windows))]
        {
            0
        }
    }
}

/// {
///   責務: [`parse_max_heavy_jobs`: 重いJobの同時数設定を解釈する]
///   処理: [1: unlimited(大小無視)は制限なし(None), 2: 1以上の整数は上限, 3: 0・負数・数値以外は拒否]
///   引数: [text: 設定値]
///   戻り値: [`Result<Option<NonZeroUsize>, String>`: Noneは無制限]
/// }
///
/// # Errors
/// 0・負数・数値以外のとき、受理できる値を示すエラーを返す。
pub fn parse_max_heavy_jobs(text: &str) -> Result<Option<NonZeroUsize>, String> {
    let text = text.trim();
    if text.eq_ignore_ascii_case("unlimited") {
        return Ok(None);
    }
    text.parse::<NonZeroUsize>().map(Some).map_err(|_| {
        format!("max_concurrent_heavy_jobs must be an integer >= 1 or unlimited (got {text:?})")
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    /// {
    ///   責務: [`priority_parses_all_values_and_rejects_others`: 取り得る3値の解析と範囲外の拒否を検証する]
    ///   処理: [1: 大小・空白を無視して3値を受理, 2: 範囲外を拒否, 3: 既定はinherit]
    ///   引数: []
    ///   戻り値: []
    /// }
    #[test]
    fn priority_parses_all_values_and_rejects_others() {
        assert_eq!(
            WorkerPriority::parse("inherit"),
            Ok(WorkerPriority::Inherit)
        );
        assert_eq!(
            WorkerPriority::parse(" Below_Normal "),
            Ok(WorkerPriority::BelowNormal)
        );
        assert_eq!(WorkerPriority::parse("IDLE"), Ok(WorkerPriority::Idle));
        for bad in ["", "high", "realtime", "below-normal"] {
            assert!(WorkerPriority::parse(bad).is_err(), "{bad}");
        }
        assert_eq!(WorkerPriority::default(), WorkerPriority::Inherit);
    }

    /// {
    ///   責務: [`inherit_is_always_effective_without_warning`: inheritは全OSで警告なしに有効でフラグ0であることを検証する]
    ///   処理: [1: effectiveが同値と警告なしを返す, 2: `creation_flags`が0]
    ///   引数: []
    ///   戻り値: []
    /// }
    #[test]
    fn inherit_is_always_effective_without_warning() {
        assert_eq!(
            WorkerPriority::Inherit.effective(),
            (WorkerPriority::Inherit, None)
        );
        assert_eq!(WorkerPriority::Inherit.creation_flags(), 0);
    }

    /// {
    ///   責務: [`non_inherit_priority_is_platform_gated`: inherit以外はWindowsだけで有効になることを検証する]
    ///   処理: [1: Windowsでは有効とフラグ値を確認, 2: それ以外はinheritへ落ちて警告とフラグ0]
    ///   引数: []
    ///   戻り値: []
    /// }
    #[test]
    fn non_inherit_priority_is_platform_gated() {
        let (effective, warning) = WorkerPriority::Idle.effective();
        if cfg!(windows) {
            assert_eq!(effective, WorkerPriority::Idle);
            assert!(warning.is_none());
            assert_eq!(WorkerPriority::Idle.creation_flags(), 0x40);
            assert_eq!(WorkerPriority::BelowNormal.creation_flags(), 0x4000);
        } else {
            assert_eq!(effective, WorkerPriority::Inherit);
            assert!(warning.is_some());
            assert_eq!(WorkerPriority::Idle.creation_flags(), 0);
        }
    }

    /// {
    ///   責務: [`max_heavy_jobs_accepts_unlimited_or_positive_integer`: 同時数設定がunlimitedか1以上の整数だけを受理することを検証する]
    ///   処理: [1: unlimitedは無制限, 2: 正の整数は上限, 3: 0・負数・非数値を拒否]
    ///   引数: []
    ///   戻り値: []
    /// }
    #[test]
    fn max_heavy_jobs_accepts_unlimited_or_positive_integer() {
        assert_eq!(parse_max_heavy_jobs("unlimited"), Ok(None));
        assert_eq!(parse_max_heavy_jobs(" UNLIMITED "), Ok(None));
        assert_eq!(parse_max_heavy_jobs("1"), Ok(NonZeroUsize::new(1)));
        assert_eq!(parse_max_heavy_jobs("4"), Ok(NonZeroUsize::new(4)));
        for bad in ["0", "-1", "", "two", "1.5"] {
            assert!(parse_max_heavy_jobs(bad).is_err(), "{bad}");
        }
    }
}
