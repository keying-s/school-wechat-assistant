"""Read Weixin 4.x databases without modifying Weixin or its files."""

from __future__ import annotations

import hashlib
import html
import importlib.util
import os
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import sqlcipher3 as sqlcipher
import zstandard

from . import config


def _load_module(path: Path):
    spec = importlib.util.spec_from_file_location("school_wcdb_key_tool", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 WCDB 读取模块：{path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _decode(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if not isinstance(value, bytes):
        return str(value)
    if value.startswith(b"\x28\xb5\x2f\xfd"):
        try:
            value = zstandard.ZstdDecompressor().decompress(value)
        except Exception:
            return ""
    return value.decode("utf-8", errors="ignore")


def _xml_value(text: str, tag: str) -> str:
    match = re.search(
        rf"<{re.escape(tag)}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{re.escape(tag)}>",
        text or "",
        flags=re.DOTALL | re.IGNORECASE,
    )
    return html.unescape(match.group(1).strip()) if match else ""


class WeChatReader:
    """Maintains verified database keys in memory and performs read-only queries."""

    def __init__(self):
        if not config.WCDB_TOOL_PATH.is_file():
            raise RuntimeError(f"缺少 WCDB 读取模块：{config.WCDB_TOOL_PATH}")
        self._tool = _load_module(config.WCDB_TOOL_PATH)
        self._tool._print = lambda *_args, **_kwargs: None
        self._tool._save_results = lambda *_args, **_kwargs: None
        self._lock = threading.RLock()
        self.account_root: Path | None = None
        self.db_root: Path | None = None
        self.message_db: Path | None = None
        self.message_dbs: list[tuple[Path, str]] = []
        self.contact_db: Path | None = None
        self.attachment_root: Path | None = None
        self._keys_by_path: dict[str, tuple[str, str]] = {}
        self._contact_names: dict[str, str] = {}
        self._sender_ids_by_db: dict[str, dict[int, str]] = {}
        self._group_db_by_id: dict[str, tuple[Path, str]] = {}
        self._file_index_at = 0.0
        self._files_by_name: dict[str, list[Path]] = {}
        self._files_by_size_ext: dict[tuple[int, str], list[Path]] = {}

    @staticmethod
    def _candidate_data_roots() -> list[Path]:
        candidates: list[Path] = []
        if config.WECHAT_DATA_ROOT:
            candidates.append(Path(config.WECHAT_DATA_ROOT))
        home = Path.home()
        candidates.extend([
            home / "xwechat_files",
            home / "Documents" / "xwechat_files",
            home / "文档" / "xwechat_files",
        ])
        appdata = Path(os.environ.get("APPDATA", ""))
        ini_dir = appdata / "Tencent" / "xwechat" / "config"
        if ini_dir.is_dir():
            for ini in ini_dir.glob("*.ini"):
                for encoding in ("utf-8", "gbk"):
                    try:
                        raw = ini.read_text(encoding=encoding).strip()
                    except UnicodeDecodeError:
                        continue
                    if raw:
                        root = Path(raw)
                        candidates.extend([root / "xwechat_files", root])
                    break
        unique: list[Path] = []
        for candidate in candidates:
            resolved = candidate.expanduser()
            if resolved not in unique:
                unique.append(resolved)
        return unique

    def discover(self) -> None:
        accounts: list[Path] = []
        for root in self._candidate_data_roots():
            if not root.is_dir():
                continue
            for child in root.iterdir():
                if child.is_dir() and (child / "db_storage" / "message" / "message_0.db").is_file():
                    accounts.append(child)
        if not accounts:
            raise RuntimeError("未发现微信 4.x 本地数据库，请确认微信已登录")
        account = max(
            accounts,
            key=lambda path: (path / "db_storage" / "message" / "message_0.db").stat().st_mtime,
        )
        self.account_root = account
        self.db_root = account / "db_storage"
        self.message_db = self.db_root / "message" / "message_0.db"
        self.contact_db = self.db_root / "contact" / "contact.db"
        self.attachment_root = account / "msg" / "file"

    def initialize(self) -> dict[str, int]:
        with self._lock:
            self.discover()
            assert self.db_root is not None
            db_files, salt_to_dbs = self._tool.collect_db_files(str(self.db_root))
            key_map = self._tool._scan_memory_raw_key(str(self.db_root), "NUL")
            mapping: dict[str, tuple[str, str]] = {}
            for rel, _path, _size, salt_hex, _page1 in db_files:
                if salt_hex in key_map:
                    mapping[rel.replace("\\", "/")] = (key_map[salt_hex], salt_hex)
            self._keys_by_path = mapping
            required = {"message/message_0.db", "contact/contact.db"}
            missing = required.difference(mapping)
            if missing:
                raise RuntimeError("关键微信数据库密钥未加载：" + ", ".join(sorted(missing)))
            self.message_dbs = []
            for rel in sorted(mapping):
                if re.fullmatch(r"message/message_\d+\.db", rel):
                    path = self.db_root / Path(rel)
                    if path.is_file():
                        self.message_dbs.append((path, rel))
            if not self.message_dbs:
                raise RuntimeError("未发现可读取的微信消息分片数据库")
            self._contact_names = self._read_contact_names()
            self._refresh_file_index(force=True)
            return {"verified": len(key_map), "total": len(salt_to_dbs)}

    def ready(self) -> bool:
        return bool(self._keys_by_path and self.message_dbs)

    def _connect(self, path: Path, rel: str):
        key_hex, salt_hex = self._keys_by_path[rel]
        conn = sqlcipher.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlcipher.Row
        conn.execute(f'PRAGMA key = "x\'{key_hex}{salt_hex}\'";')
        conn.execute("PRAGMA query_only=ON")
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return conn

    @contextmanager
    def _connection(self, path: Path, rel: str):
        """Yield a read-only SQLCipher connection and close it deterministically."""
        conn = self._connect(path, rel)
        try:
            yield conn
        finally:
            conn.close()

    def _read_contact_names(self) -> dict[str, str]:
        assert self.contact_db is not None
        names: dict[str, str] = {}
        with self._connection(self.contact_db, "contact/contact.db") as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(contact)").fetchall()}
            if "username" not in columns:
                return names
            choices = [name for name in ("remark", "nick_name", "alias", "username") if name in columns]
            expr = "COALESCE(" + ",".join(f"NULLIF({_quote(name)},'')" for name in choices) + ")"
            for username, display in conn.execute(f"SELECT username,{expr} FROM contact").fetchall():
                if username:
                    names[str(username)] = str(display or username)
        return names

    def list_groups(self) -> list[dict[str, str]]:
        if not self.ready():
            self.initialize()
        active_groups: set[str] = set()
        table_locations: dict[str, tuple[Path, str]] = {}
        sender_maps: dict[str, dict[int, str]] = {}
        with self._lock:
            for path, rel in self.message_dbs:
                with self._connection(path, rel) as conn:
                    rows = conn.execute("SELECT rowid,user_name,is_session FROM Name2Id").fetchall()
                    sender_maps[rel] = {int(row[0]): str(row[1]) for row in rows if row[1]}
                    active_groups.update(
                        str(row[1]) for row in rows
                        if row[1] and int(row[2] or 0) == 1 and str(row[1]).endswith("@chatroom")
                    )
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
                    ).fetchall():
                        table_locations[str(row[0])] = (path, rel)

            group_locations: dict[str, tuple[Path, str]] = {}
            contact_group_ids = {
                group_id for group_id in self._contact_names if group_id.endswith("@chatroom")
            }
            for group_id in contact_group_ids | active_groups:
                table = "Msg_" + hashlib.md5(group_id.encode("utf-8")).hexdigest()
                location = table_locations.get(table)
                if location:
                    group_locations[group_id] = location
            self._sender_ids_by_db = sender_maps
            self._group_db_by_id = group_locations

        group_ids = active_groups | set(group_locations)
        return [
            {"id": group_id, "name": self._contact_names.get(group_id, group_id)}
            for group_id in sorted(group_ids, key=lambda value: self._contact_names.get(value, value).casefold())
        ]

    def change_signature(self) -> tuple[int, ...]:
        signature: list[int] = []
        for path, _rel in self.message_dbs:
            db_stat = path.stat()
            wal = Path(str(path) + "-wal")
            signature.extend((db_stat.st_mtime_ns, db_stat.st_size))
            if wal.exists():
                wal_stat = wal.stat()
                signature.extend((wal_stat.st_mtime_ns, wal_stat.st_size))
            else:
                signature.extend((0, 0))
        return tuple(signature)

    def _refresh_file_index(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._file_index_at < 30:
            return
        by_name: dict[str, list[Path]] = {}
        by_size_ext: dict[tuple[int, str], list[Path]] = {}
        root = self.attachment_root
        if root and root.is_dir():
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                by_name.setdefault(path.name.casefold(), []).append(path)
                by_size_ext.setdefault((size, path.suffix.lower().lstrip(".")), []).append(path)
        self._files_by_name = by_name
        self._files_by_size_ext = by_size_ext
        self._file_index_at = now

    def _match_file(self, name: str, size: int, extension: str) -> str | None:
        self._refresh_file_index()
        candidates = self._files_by_name.get(Path(name).name.casefold(), []) if name else []
        if size:
            exact = [path for path in candidates if path.stat().st_size == size]
            if exact:
                return str(max(exact, key=lambda path: path.stat().st_mtime))
        if candidates:
            return str(max(candidates, key=lambda path: path.stat().st_mtime))
        by_size = self._files_by_size_ext.get((size, extension.lower().lstrip(".")), []) if size else []
        if len(by_size) == 1:
            return str(by_size[0])
        return None

    def resolve_file(self, name: str, size: int = 0) -> str | None:
        # Reconciliation may check hundreds of missing files; the 30-second cache
        # keeps that batch to one directory walk instead of one walk per file.
        self._refresh_file_index()
        return self._match_file(name, int(size or 0), Path(name).suffix.lstrip("."))

    def fetch_group_messages(
        self,
        group_id: str,
        cursor_time: int,
        cursor_local_id: int,
        limit: int = config.MAX_INITIAL_MESSAGES,
    ) -> list[dict[str, Any]]:
        if not self.ready():
            self.initialize()
        table = "Msg_" + hashlib.md5(group_id.encode("utf-8")).hexdigest()
        location = self._group_db_by_id.get(group_id)
        if not location:
            for path, rel in self.message_dbs:
                with self._connection(path, rel) as conn:
                    if conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                    ).fetchone():
                        location = (path, rel)
                        self._group_db_by_id[group_id] = location
                        break
        if not location:
            return []
        db_path, db_rel = location
        with self._lock, self._connection(db_path, db_rel) as conn:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if not exists:
                return []
            if db_rel not in self._sender_ids_by_db:
                self._sender_ids_by_db[db_rel] = {int(row[0]): str(row[1]) for row in conn.execute(
                    "SELECT rowid,user_name FROM Name2Id"
                ).fetchall() if row[1]}
            sender_map = self._sender_ids_by_db[db_rel]
            rows = conn.execute(
                f"""
                SELECT local_id,server_id,local_type,real_sender_id,create_time,
                       download_status,source,message_content,compress_content,packed_info_data
                FROM {_quote(table)}
                WHERE create_time>? OR (create_time=? AND local_id>?)
                ORDER BY create_time,local_id LIMIT ?
                """,
                (int(cursor_time), int(cursor_time), int(cursor_local_id), int(limit)),
            ).fetchall()

        messages: list[dict[str, Any]] = []
        for row in rows:
            local_type = int(row[2] or 0)
            base_type = local_type & 0xFFFF
            app_subtype = local_type >> 32 if base_type == 49 else 0
            source = _decode(row[6])
            content_candidates = [_decode(row[8]), _decode(row[7]), source]
            content = next((value for value in content_candidates if value), "")
            sender_id = sender_map.get(int(row[3] or 0), "")
            sender_name = self._contact_names.get(sender_id, sender_id or "未知发送者")
            message_type = {
                1: "text", 3: "image", 34: "voice", 43: "video",
                47: "sticker", 48: "location", 49: "app", 10000: "system",
            }.get(base_type, f"type_{base_type}")
            file_name = file_md5 = local_path = None
            file_size = None
            download_state = "none"

            if base_type == 49:
                xml = next((value for value in content_candidates if "<appmsg" in value), content)
                title = _xml_value(xml, "title")
                description = _xml_value(xml, "des")
                url = _xml_value(xml, "url")
                if app_subtype == 6:
                    message_type = "file"
                    file_name = title or "未命名文件"
                    size_text = _xml_value(xml, "totallen")
                    file_size = int(size_text) if size_text.isdigit() else 0
                    extension = _xml_value(xml, "fileext") or Path(file_name).suffix.lstrip(".")
                    file_md5 = _xml_value(xml, "md5") or None
                    local_path = self._match_file(file_name, file_size, extension)
                    download_state = "available" if local_path else "missing"
                    content = f"文件：{file_name}"
                else:
                    content = "\n".join(part for part in (title, description, url) if part).strip() or content
            elif base_type == 1:
                prefix = sender_id + ":\n" if sender_id else ""
                if prefix and content.startswith(prefix):
                    content = content[len(prefix):]

            messages.append({
                "local_id": int(row[0]),
                "server_id": str(row[1] or ""),
                "sender_id": sender_id,
                "sender_name": sender_name,
                "create_time": int(row[4] or 0),
                "message_type": message_type,
                "app_subtype": int(app_subtype),
                "content": content[:12000],
                "file_name": file_name,
                "file_size": file_size,
                "file_md5": file_md5,
                "local_path": local_path,
                "download_state": download_state,
                "download_status": int(row[5] or 0),
            })
        return messages
