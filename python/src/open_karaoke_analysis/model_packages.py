"""Data-only discovery of user-placed model packages; no package code is ever imported or run."""

import json
from itertools import islice
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .adapters import ModelAdapter
from .contracts import Role, ServiceError

MANIFEST_VERSION = 1
MANIFEST_NAME = "manifest.json"
MAX_MANIFEST_BYTES = 64 * 1024
MAX_PACKAGE_DIRECTORIES = 256
MAX_SCANNED_ENTRIES = 4096
MAX_DISPLAY_ID = 64
ID_PATTERN = r"[a-z0-9][a-z0-9._-]{0,63}"

PackageStatus = Literal["available", "unsupported_backend", "role_mismatch", "duplicate", "invalid"]


class PackageManifest(BaseModel):
    """
    {
      責務: [PackageManifest: models/<id>/manifest.json のmanifest_version 1を表す]
      フィールド: [
        id: パッケージID。フォルダ名と一致する,
        name: 表示名,
        role: Analysis Serviceのrole名,
        backend: 結び付ける登録済みAdapterのID,
        version: パッケージ版,
        languages: 対応言語,
        license / license_url / commercial_use / redistribution: 利用者向けの情報表示。審査はしない,
        capabilities: 機能名と有無
      ]
    }
    """

    model_config = ConfigDict(extra="ignore", strict=True)
    manifest_version: Literal[1]
    id: str = Field(pattern=f"^{ID_PATTERN}$")
    name: str = Field(min_length=1, max_length=128)
    role: Role
    backend: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=64)
    languages: list[Annotated[str, Field(max_length=16)]] = Field(
        default_factory=list, max_length=16
    )
    license: str | None = Field(default=None, max_length=128)
    license_url: str | None = Field(default=None, max_length=512)
    commercial_use: bool | None = None
    redistribution: bool | None = None
    capabilities: dict[str, bool] = Field(default_factory=dict, max_length=16)

    @field_validator("capabilities")
    @classmethod
    def _short_capability_names(cls, value: dict[str, bool]) -> dict[str, bool]:
        if any(len(name) > 48 for name in value):
            raise ValueError("capability name is too long")
        return value


class PackageEntry(BaseModel):
    """
    {
      責務: [PackageEntry: 発見した1パッケージの一覧表示用の状態]
      フィールド: [
        id: 有効ならmanifestのID、不正ならフォルダ名,
        status: 利用可否の分類,
        origin: 常にuser_supplied。公式検証済みとは区別する,
        error: statusが利用不可のときの短い理由。パスは含めない,
        その他: manifestの表示情報。不正なときはnull
      ]
    }
    """

    id: str
    status: PackageStatus
    origin: Literal["user_supplied"] = "user_supplied"
    error: str | None = None
    name: str | None = None
    role: Role | None = None
    backend: str | None = None
    version: str | None = None
    languages: list[str] = Field(default_factory=list)
    license: str | None = None
    license_url: str | None = None
    commercial_use: bool | None = None
    redistribution: bool | None = None
    capabilities: dict[str, bool] = Field(default_factory=dict)


def _invalid(directory: str, reason: str) -> PackageEntry:
    shown = "".join(c if c.isprintable() else "?" for c in directory)[:MAX_DISPLAY_ID]
    return PackageEntry(id=shown, status="invalid", error=reason)


def _read_manifest(directory: Path) -> PackageManifest | str:
    """
    {
      責務: [_read_manifest: 1フォルダのmanifestを安全に読み、検証結果か失敗理由を返す]
      処理: [1: 通常ファイルで64KiB以下か確認する, 2: UTF-8のJSONとして読む, 3: schemaで検証する]
      引数: [directory: models直下のパッケージフォルダ]
      戻り値: [PackageManifest。失敗時は理由の文字列]
    }
    """
    path = directory / MANIFEST_NAME
    try:
        if path.is_symlink() or not path.is_file():
            return "manifest.json is not a regular file"
        # stat後の差し替えや擬似ファイルに備え、開いたあとも上限+1バイトまでしか読まない。
        with path.open("rb") as stream:
            raw = stream.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) > MAX_MANIFEST_BYTES:
            return "manifest.json is larger than 64 KiB"
        text = raw.decode("utf-8")
        value = json.loads(text, parse_constant=_reject_constant)
    except (OSError, UnicodeDecodeError):
        return "manifest.json cannot be read as UTF-8"
    except (ValueError, RecursionError):
        return "manifest.json is not valid JSON"
    if isinstance(value, dict) and value.get("manifest_version") not in (None, MANIFEST_VERSION):
        return "manifest_version is not supported"
    try:
        return PackageManifest.model_validate_json(text)
    except (ValidationError, RecursionError):
        return "manifest.json does not match manifest_version 1"


def _reject_constant(name: str) -> None:
    raise ValueError(name)


def discover_packages(root: Path, adapters: dict[str, ModelAdapter]) -> list[PackageEntry]:
    """
    {
      責務: [discover_packages: models/配下のモデルパッケージを検出し、利用可否つきで一覧にする]
      処理: [
        1: models/直下の先頭4096件の名前を並べ、manifest.jsonを持つ実フォルダを最大256件調べる,
        2: 各manifestを検証する。不正でも他のパッケージと全体の処理は続ける,
        3: IDが重複するもの、組み込みAdapterのIDと衝突するものはすべてduplicateにする,
        4: backendが登録済みAdapterで、roleが一致するものだけをavailableにする
      ]
      引数: [
        root: Analysis Serviceのartifact root,
        adapters: 登録済みAdapterのID別辞書。コードは呼び出さない
      ]
      戻り値: [PackageEntryのリスト。重みだけのフォルダ(manifestなし)は含めない]
      エラー: [ServiceError: model_packages_unavailable。models/が読めない(権限等)。無いだけなら空]
    }
    """
    models = root / "models"
    try:
        names = sorted(p.name for p in islice(models.iterdir(), MAX_SCANNED_ENTRIES))
    except (FileNotFoundError, NotADirectoryError):
        return []
    except OSError as error:
        raise ServiceError(
            "model_packages_unavailable", "The models directory cannot be read", 503
        ) from error
    candidates: list[Path] = []
    for name in names:
        directory = models / name
        try:
            if directory.is_symlink() or not directory.is_dir():
                continue
            manifest_path = directory / MANIFEST_NAME
            if not (manifest_path.is_symlink() or manifest_path.exists()):
                continue
        except OSError:
            continue
        candidates.append(directory)
        if len(candidates) == MAX_PACKAGE_DIRECTORIES:
            break
    entries: list[tuple[PackageEntry, PackageManifest | None]] = []
    for directory in candidates:
        manifest = _read_manifest(directory)
        if isinstance(manifest, str):
            entries.append((_invalid(directory.name, manifest), None))
        elif manifest.id != directory.name:
            entries.append((_invalid(directory.name, "id does not match the folder name"), None))
        else:
            entries.append((_describe(manifest, adapters), manifest))
    seen: dict[str, int] = {}
    for _entry, manifest in entries:
        if manifest is not None:
            seen[manifest.id] = seen.get(manifest.id, 0) + 1
    result = []
    for entry, manifest in entries:
        if manifest is not None and (seen[manifest.id] > 1 or manifest.id in adapters):
            entry = entry.model_copy(
                update={"status": "duplicate", "error": "id is already used by another model"}
            )
        result.append(entry)
    return result


def _describe(manifest: PackageManifest, adapters: dict[str, ModelAdapter]) -> PackageEntry:
    adapter = adapters.get(manifest.backend)
    status: PackageStatus = "available"
    error = None
    if adapter is None:
        status, error = "unsupported_backend", "backend is not an installed adapter"
    elif adapter.info.role != manifest.role:
        status, error = "role_mismatch", "backend does not provide this role"
    return PackageEntry(
        id=manifest.id,
        status=status,
        error=error,
        name=manifest.name,
        role=manifest.role,
        backend=manifest.backend,
        version=manifest.version,
        languages=manifest.languages,
        license=manifest.license,
        license_url=manifest.license_url,
        commercial_use=manifest.commercial_use,
        redistribution=manifest.redistribution,
        capabilities=manifest.capabilities,
    )
