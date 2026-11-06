"""宣言コメントの差分ゲートを、gitへ接続せずに検証するテスト。"""

from scripts import check_declaration_comments as checker


def test_added_lines_tracks_new_file_positions():
    """差分hunkの旧行番号と追加行番号を混同しない。"""
    diff = """diff --git a/core/src/example.rs b/core/src/example.rs
--- a/core/src/example.rs
+++ b/core/src/example.rs
@@ -1,2 +1,3 @@
 keep
+pub struct NewThing;
+pub fn new_fn() {}
"""
    assert checker.parse_added_lines(diff) == {"core/src/example.rs": {2, 3}}


def test_python_checks_new_declarations_but_not_existing_body_edits():
    """既存のコメント不足を無関係な差分の失敗へ波及させない。"""
    source = '''
class Existing:
    def existing(self):
        return 1

class Added:
    """{ 責務: [状態の所有] フィールド: [value: 保存する値] }"""
    def method(self):
        """{ 責務: [操作] 処理: [1: 返す] 引数: [self: 所有者] 戻り値: [数値] }"""
        return 2
'''
    assert checker.check_python(source, {4}) == []
    assert checker.check_python(source, {6, 8}) == []
    errors = checker.check_python(source, {2})
    assert len(errors) == 1 and errors[0][1] == "Existing"
    assert "フィールド:" in errors[0][2]


def test_python_rejects_missing_function_comment_fields():
    """docstringが存在しても責務・処理・入出力が欠ければ拒否する。"""
    source = '''
def work(input_value):
    """従来型の説明のみ。"""
    return input_value
'''
    errors = checker.check_python(source, {2})
    assert len(errors) == 1
    assert set(errors[0][2]) == {"責務:", "処理:", "引数:", "戻り値:"}


def test_rust_attributes_preserve_adjacent_documentation():
    """derive属性の上にあるRustの///を当該型のコメントとして扱う。"""
    valid = """/// {
/// 責務: [状態を所有]
/// フィールド: [value: 値]
/// }
#[derive(Debug)]
pub struct Owned { value: u64 }
"""
    assert checker.check_slash_docs(valid, {6}, "rs") == []
    bad = "pub struct Missing { value: u64 }\n"
    assert checker.check_slash_docs(bad, {1}, "rs")[0][2] == checker.REQUIRED_TYPE


def test_csharp_comment_fields_and_existing_lines():
    """C#のXMLドキュメント形式で責務・処理・引数・戻り値を検査する。"""
    source = """public sealed record Missing(string Name);
/// {
/// 責務: [Coreへ要求する]
/// 処理: [1: 応答を読む]
/// 引数: [id: 対象]
/// 戻り値: [成功結果]
/// }
public Task<Response> CallAsync(int id) => Task.FromResult(new Response());
"""
    assert checker.check_slash_docs(source, {7}, "cs") == []
    errors = checker.check_slash_docs(source, {1}, "cs")
    assert len(errors) == 1 and "フィールド:" in errors[0][2]


def test_source_filter_does_not_scan_fixture_or_test_files():
    """生産コードとテスト・Fixtureを同じルールで誤検査しない。"""
    assert checker.production_language("core/src/audio_engine.rs") == "rs"
    assert checker.production_language("python/src/open_karaoke_analysis/song.py") == "py"
    assert checker.production_language("gui/OpenKaraoke.Client/CoreApiClient.cs") == "cs"
    assert checker.production_language("python/tests/test_song.py") is None
    assert checker.production_language("core/tests/audio_clock.rs") is None
    assert checker.production_language("scripts/check_declaration_comments.py") is None
