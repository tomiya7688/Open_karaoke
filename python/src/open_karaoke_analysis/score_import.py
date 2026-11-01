"""Importer API for external score files (Standard MIDI File and future karaoke formats)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .contracts import ServiceError

MAX_IMPORT_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class ImportedScore:
    """
    {
      責務: [ImportedScore: 外部譜面から変換した正規notes文書と取込evidenceを不変の組で保持する]
      フィールド: [
        importer_id: 変換に使ったImporterの識別子,
        notes_document: Song Data v1のnotes.json文書,
        evidence_document: 取込方針・tempo map・除外件数などの説明用文書
      ]
    }
    """

    importer_id: str
    notes_document: dict[str, Any]
    evidence_document: dict[str, Any]


class ScoreImporter(Protocol):
    """
    {
      責務: [ScoreImporter: 1つの外部譜面形式を内部notes.jsonへ変換する差し替え可能な契約]
      フィールド: [
        format_id: registryで一意な形式識別子（例: smf）
      ]
    }
    """

    format_id: str

    def sniff(self, data: bytes) -> bool:
        """
        {
          責務: [sniff: 先頭bytesがこの形式らしいかを副作用なしで判定する]
          処理: [1: magic等の固定長の印だけを確認する（例外を送出せず、短い入力はFalse）]
          引数: [data: 取込対象ファイルのbytes]
          戻り値: [この形式として扱えるならTrue]
        }
        """
        ...

    def import_score(self, data: bytes, options: Mapping[str, Any]) -> ImportedScore:
        """
        {
          責務: [import_score: bytesを検証して正規notes文書へ変換する]
          処理: [1: optionsを検証する, 2: bytesを有界に解析する, 3: notes/evidenceを返す]
          引数: [data: 取込対象ファイルのbytes, options: 形式固有の設定]
          戻り値: [ImportedScore]
          エラー: [ServiceError: 形式固有の判別可能なcode]
        }
        """
        ...


class ImporterRegistry:
    """
    {
      責務: [ImporterRegistry: 形式IDからImporterを引き、明示指定または自動判定で取込を委譲する]
      フィールド: [_importers: format_idからScoreImporterへの対応]
    }
    """

    def __init__(self) -> None:
        """
        {
          責務: [__init__: 空のregistryを作る]
          処理: [1: 内部対応表を初期化する]
          引数: []
          戻り値: []
        }
        """
        self._importers: dict[str, ScoreImporter] = {}

    def register(self, importer: ScoreImporter) -> None:
        """
        {
          責務: [register: Importerを一意なformat_idで登録する]
          処理: [1: format_idの重複を拒否する, 2: 登録する]
          引数: [importer: 登録するImporter]
          戻り値: []
          エラー: [ValueError: 同じformat_idが登録済み（プログラム構成の誤り）]
        }
        """
        if importer.format_id in self._importers:
            raise ValueError(f"duplicate score importer: {importer.format_id}")
        self._importers[importer.format_id] = importer

    def formats(self) -> list[str]:
        """
        {
          責務: [formats: 登録済み形式IDを決定的な順序で返す]
          処理: [1: 形式IDを辞書順に並べる]
          引数: []
          戻り値: [形式IDのlist]
        }
        """
        return sorted(self._importers)

    def import_score(
        self,
        data: bytes,
        options: Mapping[str, Any] | None = None,
        *,
        format_id: str | None = None,
    ) -> ImportedScore:
        """
        {
          責務: [import_score: 指定または判定した形式のImporterへ取込を委譲する]
          処理: [
            1: 入力型と最大サイズを検証する,
            2: format_id指定時はその形式、未指定時はsniffが一致した唯一の形式を選ぶ,
            3: 選んだImporterで変換する
          ]
          引数: [data: ファイルbytes, options: 形式固有設定, format_id: 明示する形式ID]
          戻り値: [ImportedScore]
          エラー: [
            score_import_too_large: MAX_IMPORT_BYTESを超える,
            score_import_unknown_format: 未登録の形式または判定不能,
            score_import_ambiguous_format: 複数形式がsniffに一致
          ]
        }
        """
        if not isinstance(data, bytes | bytearray):
            raise ServiceError("score_import_invalid_input", "Score import input must be bytes")
        if len(data) > MAX_IMPORT_BYTES:
            raise ServiceError(
                "score_import_too_large",
                f"Score file exceeds {MAX_IMPORT_BYTES} bytes",
            )
        if format_id is not None:
            importer = self._importers.get(format_id)
            if importer is None:
                raise ServiceError(
                    "score_import_unknown_format", f"Unknown score format: {format_id}"
                )
        else:
            matches = [item for _, item in sorted(self._importers.items()) if item.sniff(data)]
            if not matches:
                raise ServiceError(
                    "score_import_unknown_format", "Score file format is not recognized"
                )
            if len(matches) > 1:
                raise ServiceError(
                    "score_import_ambiguous_format",
                    "Score file matches several formats: "
                    + ", ".join(item.format_id for item in matches),
                )
            importer = matches[0]
        return importer.import_score(bytes(data), options or {})


def default_registry(
    extra: Callable[[ImporterRegistry], None] | None = None,
) -> ImporterRegistry:
    """
    {
      責務: [default_registry: 公式に同梱するImporterを登録したregistryを作る]
      処理: [1: SMF Importerを登録する, 2: 任意の追加登録関数を呼ぶ]
      引数: [extra: 追加形式を登録する関数（任意）]
      戻り値: [ImporterRegistry]
    }
    """
    from .midi_import import SmfImporter

    registry = ImporterRegistry()
    registry.register(SmfImporter())
    if extra is not None:
        extra(registry)
    return registry
