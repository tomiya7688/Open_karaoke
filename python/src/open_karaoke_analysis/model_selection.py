"""Persisted per-role model choice. Data only: the file names a model, it never runs one."""

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from .adapters import ModelAdapter
from .contracts import Role, ServiceError
from .model_packages import ID_PATTERN, discover_packages

SELECTION_NAME = "model_selection.json"
SELECTION_VERSION = 1
MAX_SELECTION_BYTES = 16 * 1024
_ID = re.compile(f"^{ID_PATTERN}$")
_LOCK = Lock()


def _reject_constant(name: str) -> None:
    raise ValueError(name)


def selectable_models(root: Path, adapters: dict[str, ModelAdapter]) -> dict[str, set[str]]:
    """
    {
      責務: [selectable_models: roleごとに今選べるモデルIDを返す(組み込みAdapterと利用可パッケージ)]
      処理: [1: 登録済みAdapterのIDをroleへ振り分ける, 2: availableのパッケージIDを同じroleへ足す]
      引数: [root: artifact root, adapters: 登録済みAdapterのID別辞書]
      戻り値: [role名 -> 選べるIDの集合]
      エラー: [ServiceError: model_packages_unavailable。models/が読めない]
    }
    """
    choices: dict[str, set[str]] = {}
    for adapter_id, adapter in adapters.items():
        choices.setdefault(adapter.info.role.value, set()).add(adapter_id)
    for entry in discover_packages(root, adapters):
        if entry.status == "available" and entry.role is not None:
            choices.setdefault(entry.role.value, set()).add(entry.id)
    return choices


def _read_raw(root: Path) -> dict[str, str]:
    """
    {
      責務: [_read_raw: 保存ファイルを安全に読み、形式が正しいroleとIDの組だけ返す]
      処理: [
        1: 通常ファイルで16KiB以下か確認する(リンク・巨大・読めないものは空として扱う),
        2: UTF-8のJSONとして読み、versionと形を確認する,
        3: 既知のroleと、IDの書式に合う文字列だけ残す。パスや未知の値は捨てる
      ]
      引数: [root: artifact root]
      戻り値: [role名 -> ID。無い・壊れているときは空]
    }
    """
    path = root / SELECTION_NAME
    try:
        if path.is_symlink() or not path.is_file():
            return {}
        with path.open("rb") as stream:
            raw = stream.read(MAX_SELECTION_BYTES + 1)
        if len(raw) > MAX_SELECTION_BYTES:
            return {}
        value = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (OSError, ValueError, RecursionError):
        return {}
    if not isinstance(value, dict) or value.get("version") != SELECTION_VERSION:
        return {}
    stored = value.get("selection")
    if not isinstance(stored, dict):
        return {}
    roles = {role.value for role in Role}
    result: dict[str, str] = {}
    for role, model_id in stored.items():
        if role in roles and isinstance(model_id, str) and _ID.fullmatch(model_id):
            result[role] = model_id
    return result


def read_selection(root: Path, adapters: dict[str, ModelAdapter]) -> dict[str, str]:
    """
    {
      責務: [read_selection: 保存済みの選択のうち、今も選べるものだけを返す]
      処理: [1: 保存ファイルを読む, 2: 消えた・利用不可の選択は結果から外す(ファイルは変えない)]
      引数: [root: artifact root, adapters: 登録済みAdapterのID別辞書]
      戻り値: [role名 -> 選択中のモデルID]
      エラー: [ServiceError: model_packages_unavailable]
    }
    """
    choices = selectable_models(root, adapters)
    return {
        role: model_id
        for role, model_id in sorted(_read_raw(root).items())
        if model_id in choices.get(role, set())
    }


def _write(root: Path, stored: dict[str, str]) -> None:
    payload = json.dumps(
        {"version": SELECTION_VERSION, "selection": dict(sorted(stored.items()))},
        ensure_ascii=True,
        indent=2,
    ).encode("utf-8")
    descriptor, temp_name = tempfile.mkstemp(prefix=".model_selection.", suffix=".tmp", dir=root)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, root / SELECTION_NAME)
    except OSError as error:
        Path(temp_name).unlink(missing_ok=True)
        raise ServiceError(
            "model_selection_unavailable", "The model selection cannot be saved", 503
        ) from error


def set_selection(
    root: Path, adapters: dict[str, ModelAdapter], role: Role, model_id: str | None
) -> dict[str, str]:
    """
    {
      責務: [set_selection: 1 roleの選択を保存する。nullなら選択を解除する]
      処理: [
        1: 今選べるモデルか確認する(未知・利用不可・別roleのIDは拒否),
        2: 既存の保存内容(今は選べない記録を含む)へ1件だけ反映する,
        3: 一時ファイルへ書いて置き換える(途中で壊れた内容を残さない)
      ]
      引数: [root, adapters: 登録済みAdapter辞書, role: 対象role, model_id: 選ぶID(解除はnull)]
      戻り値: [保存後の有効な選択(role名 -> ID)]
      エラー: [ServiceError: model_not_selectable, model_selection_unavailable]
    }
    """
    with _LOCK:
        if model_id is not None and model_id not in selectable_models(root, adapters).get(
            role.value, set()
        ):
            raise ServiceError(
                "model_not_selectable", "The model is not available for this role", 409
            )
        stored = _read_raw(root)
        if model_id is None:
            stored.pop(role.value, None)
        else:
            stored[role.value] = model_id
        _write(root, stored)
        return read_selection(root, adapters)


@dataclass(frozen=True)
class SelectionChoice:
    """
    {
      責務: [SelectionChoice: 保存済み選択をJobの実行先へ解決した結果]
      フィールド: [
        stored_id: 保存されていた選択ID。未選択ならNone,
        adapter_id: 使える場合に実行する登録済みAdapterのID。使えないときはNone,
        fallback_reason: stored_idがあるのにadapter_idがNoneのときの短い理由
      ]
    }
    """

    stored_id: str | None = None
    adapter_id: str | None = None
    fallback_reason: str | None = None


def resolve_selection(root: Path, adapters: dict[str, ModelAdapter], role: Role) -> SelectionChoice:
    """
    {
      責務: [resolve_selection: 保存済みの選択を、Jobで動かす登録済みAdapterIDへ解決する]
      処理: [
        1: 保存ファイルからそのroleのIDを読む(無ければ未選択として返す),
        2: 組み込みAdapterのIDで、roleが一致するならそのAdapterを使う,
        3: それ以外はavailableなパッケージのmanifestのbackendを使う(データだけ。コードは動かさない),
        4: どれにも当たらない・models/が読めない場合は理由つきで未解決にする(例外にしない)
      ]
      引数: [root: artifact root, adapters: 登録済みAdapter辞書, role: Jobのrole]
      戻り値: [SelectionChoice。呼び出し側は未解決なら既定Adapterへ戻す]
    }
    """
    stored = _read_raw(root).get(role.value)
    if stored is None:
        return SelectionChoice()
    adapter = adapters.get(stored)
    if adapter is not None:
        if adapter.info.role == role:
            return SelectionChoice(stored, stored)
        return SelectionChoice(stored, None, "selected model is not available for this role")
    try:
        entries = discover_packages(root, adapters)
    except ServiceError:
        return SelectionChoice(stored, None, "model packages cannot be read")
    for entry in entries:
        if entry.id == stored and entry.status == "available" and entry.role == role:
            backend = entry.backend
            if backend in adapters and adapters[backend].info.role == role:
                return SelectionChoice(stored, backend)
    return SelectionChoice(stored, None, "selected model is no longer available")
