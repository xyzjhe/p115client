#!/usr/bin/env python3
# encoding: utf-8

__author__ = "ChenyangGao <https://chenyanggao.github.io>"
__all__ = [
    "updatedb_initdb", "updatedb", "updatedb_life_iter", 
    "updatedb_history_iter", "P115QueryDB", 
]
__doc__ = "这个模块提供了一些和更新数据库有关的函数"

from collections.abc import (
    AsyncIterator, Callable, Coroutine, Iterable, Iterator, Sequence, 
)
from csv import writer
from itertools import batched
from math import inf
from ntpath import normpath
from os import PathLike
from os.path import expanduser
from sqlite3 import connect, Connection, Cursor
from time import time
from typing import overload, Any, Literal
from warnings import warn

from asynctools import ensure_async
from errno2 import errno
from iterutils import (
    bfs_gen, chunked, foreach, run_gen_step, run_gen_step_iter, 
    with_iter_next, Yield, 
)
from orjson import dumps, loads
from p115client import P115Client, P115Warning
from p115pickcode import to_id
from posixpatht import path_is_dir_form, escape, splits
from sqlitetools import execute, find, query, transact, upsert_items, AutoCloseConnection

from .attr import get_ancestors_to_cid
from .iterdir import iter_nodes_using_event, traverse_tree
from .life import iter_life_behavior_list
from .history import iter_history_list


def updatedb_initdb(con: Connection | Cursor, /) -> Cursor:
    """初始化数据库，然后返回游标
    """
    sql = """\
-- 修改日志模式为 WAL (Write Ahead Log)
PRAGMA journal_mode = WAL;

-- data 表，用来保存数据
CREATE TABLE IF NOT EXISTS data (
    id INTEGER NOT NULL PRIMARY KEY,      -- 主键
    parent_id INTEGER NOT NULL DEFAULT 0, -- 上级目录的 id
    name TEXT NOT NULL,                   -- 名字
    sha1 TEXT NOT NULL DEFAULT '',        -- 文件的 sha1 散列值
    size INTEGER NOT NULL DEFAULT 0,      -- 文件大小
    pickcode TEXT NOT NULL DEFAULT '',    -- 提取码，下载等操作时需要用到
    is_dir INTEGER NOT NULL DEFAULT 1 CHECK(is_dir IN (0, 1)), -- 是否目录
    is_alive INTEGER NOT NULL DEFAULT 1 CHECK(is_alive IN (0, 1)), -- 是否存活（存活即是不是删除状态）
    extra BLOB DEFAULT NULL,              -- 额外的数据
    created_at TIMESTAMP DEFAULT (unixepoch('subsec')), -- 创建时间
    updated_at TIMESTAMP DEFAULT (CAST(STRFTIME('%s', 'now') AS INTEGER))  -- 更新时间
);

-- life 表，用来保存操作事件
CREATE TABLE IF NOT EXISTS life (
    id INTEGER NOT NULL PRIMARY KEY, -- 文件或目录的 id
    data JSON NOT NULL, -- 数据
    created_at TIMESTAMP DEFAULT (unixepoch('subsec')) -- 创建时间
);

-- history 表，用来保存历史记录
CREATE TABLE IF NOT EXISTS history (
    id INTEGER NOT NULL PRIMARY KEY, -- 文件或目录的 id
    data JSON NOT NULL, -- 数据
    created_at TIMESTAMP DEFAULT (unixepoch('subsec')) -- 创建时间
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_data_pid ON data(parent_id);
CREATE INDEX IF NOT EXISTS idx_data_utime ON data(updated_at);

-- data 表的记录发生更新，自动更新它的更新时间
CREATE TRIGGER IF NOT EXISTS trg_data_update
AFTER UPDATE ON data
FOR EACH ROW
BEGIN
    SELECT CASE
        WHEN NEW.updated_at < OLD.updated_at THEN RAISE(IGNORE)
    END;
    UPDATE data SET updated_at = CAST(STRFTIME('%s', 'now') AS INTEGER) WHERE id = NEW.id AND NEW.updated_at = OLD.updated_at;
END;

-- fs_event 表，用来保存文件系统变更（由 data 表触发）
CREATE TABLE IF NOT EXISTS fs_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT, -- 事件 id
    event TEXT NOT NULL,                  -- 事件类型：add（增）、remove（删）、rename（改名）、move（移动）
    file_id INTEGER NOT NULL,             -- 文件或目录的 id，此 id 必在 `data` 表中
    pid0 INTEGER NOT NULL DEFAULT -1,     -- 变更前上级目录的 id
    pid1 INTEGER NOT NULL DEFAULT -1,     -- 变更后上级目录的 id
    name0 TEXT NOT NULL DEFAULT '',       -- 变更前的名字
    name1 TEXT NOT NULL DEFAULT '',       -- 变更后的名字
    created_at TIMESTAMP DEFAULT (unixepoch('subsec')) -- 创建时间
);

-- data 表发生插入
CREATE TRIGGER IF NOT EXISTS trg_data_insert
AFTER INSERT ON data
FOR EACH ROW
BEGIN
    INSERT INTO fs_event(event, file_id, pid1, name1) VALUES (
        'add', NEW.id, NEW.parent_id, NEW.name
    );
END;

-- data 表发生还原
CREATE TRIGGER IF NOT EXISTS trg_data_revoke
AFTER UPDATE ON data
FOR EACH ROW WHEN (NOT OLD.is_alive AND NEW.is_alive)
BEGIN
    INSERT INTO fs_event(event, file_id, pid1, name1) VALUES (
        'add', NEW.id, NEW.parent_id, NEW.name
    );
END;

-- data 表发生移除
CREATE TRIGGER IF NOT EXISTS trg_data_remove
AFTER UPDATE ON data
FOR EACH ROW WHEN (OLD.is_alive AND NOT NEW.is_alive)
BEGIN
    INSERT INTO fs_event(event, file_id, pid0, name0) VALUES (
        'remove', OLD.id, OLD.parent_id, OLD.name
    );
END;

-- data 表发生改名或移动
CREATE TRIGGER IF NOT EXISTS trg_data_change
AFTER UPDATE ON data
FOR EACH ROW WHEN (OLD.is_alive AND NEW.is_alive)
BEGIN
    INSERT INTO fs_event(event, file_id, pid0, pid1, name0, name1)
    SELECT
        'move', OLD.id, OLD.parent_id, NEW.parent_id, OLD.name, OLD.name
    WHERE OLD.parent_id != NEW.parent_id;

    INSERT INTO fs_event(event, file_id, pid0, pid1, name0, name1) 
    SELECT * FROM (
        SELECT
            'rename', NEW.id, NEW.parent_id, NEW.parent_id, OLD.name, NEW.name
        WHERE OLD.name != NEW.name
    );
END;"""
    return con.executescript(sql)


def wrap_async(func, async_: bool = False, /, threaded: bool = False):
    if async_:
        return ensure_async(func, threaded=threaded)
    else:
        return func


def _init_client(
    client: str | PathLike | P115Client, 
    dbfile: None | str | PathLike | Connection | Cursor = None, 
) -> tuple[P115Client, Connection | Cursor]:
    if isinstance(client, (str, PathLike)):
        client = P115Client(client, check_for_relogin=True)
    if client.login_app() in ("web", "desktop", "harmony"):
        warn(
            f'app within ("web", "desktop", "harmony") is not recommended, it will be replaced by "apple_tv" cookies', 
            category=P115Warning, 
        )
        client.login_another_app("apple_tv", replace=True)
    if not dbfile:
        dbfile = f"p115db-{client.user_id}.db"
    if isinstance(dbfile, (Connection, Cursor)):
        con = dbfile
    else:
        con = connect(
            dbfile, 
            check_same_thread=False, 
            factory=AutoCloseConnection, 
            timeout=inf, 
            uri=isinstance(dbfile, str) and dbfile.startswith("file:"), 
        )
        updatedb_initdb(con)
    return client, con


def has_id(con: Connection | Cursor, id: int, /) -> int:
    sql = "SELECT 1 FROM data WHERE id = ? AND is_alive"
    return find(con, sql, (id,), default=0)


def event_normalize_attr(event: dict, /) -> dict:
    sha1 = event["sha1"]
    return {
        "id": int(event["file_id"]), 
        "parent_id": int(event["parent_id"]), 
        "name": event["file_name"], 
        "sha1": sha1, 
        "size": int(event.get("file_size") or 0), 
        "pickcode": event["pick_code"], 
        "is_dir": not sha1, 
        "is_alive": event["type"] != 22, 
        "updated_at": int(event["create_time"]), 
    }


@overload
def load_missing_ancestors(
    client: P115Client, 
    con: Connection | Cursor, 
    attrs: list[dict], 
    cooldown: float = 0.2, 
    app: str = "android", 
    *, 
    async_: Literal[False] = False, 
    **request_kwargs, 
) -> list[dict]:
    ...
@overload
def load_missing_ancestors(
    client: P115Client, 
    con: Connection | Cursor, 
    attrs: list[dict], 
    cooldown: float = 0.2, 
    app: str = "android", 
    *, 
    async_: Literal[True], 
    **request_kwargs, 
) -> Coroutine[Any, Any, list[dict]]:
    ...
def load_missing_ancestors(
    client: P115Client, 
    con: Connection | Cursor, 
    attrs: list[dict], 
    cooldown: float = 0.2, 
    app: str = "android", 
    *, 
    async_: Literal[False, True] = False, 
    **request_kwargs, 
) -> list[dict] | Coroutine[Any, Any, list[dict]]:
    def gen_step():
        seen_ids: set[int] = {a["id"] for a in attrs}
        ancestors: list[dict] = []
        add_to_seen = seen_ids.add
        add_ancestor = ancestors.append
        def add(attr: dict, /):
            add_to_seen(attr["id"])
            add_ancestor(attr)
        while pids := [
            pid for a in attrs 
            if (pid := a["parent_id"]) and not (pid in seen_ids or has_id(con, pid))
        ]:
            yield foreach(
                add, 
                iter_nodes_using_event(
                    client, 
                    pids, 
                    type="doc", 
                    normalize_attr=event_normalize_attr, 
                    id_to_dirnode=..., 
                    cooldown=cooldown, 
                    app=app, 
                    async_=async_, 
                    **request_kwargs, 
                ), 
            )
        return ancestors
    return run_gen_step(gen_step, async_)


@overload
def updatedb(
    client: str | PathLike | P115Client, 
    dbfile: None | str | PathLike | Connection | Cursor = None, 
    cid: int | str = 0, 
    app: str = "android", 
    *, 
    async_: Literal[False] = False, 
    **request_kwargs, 
) -> int:
    ...
@overload
def updatedb(
    client: str | PathLike | P115Client, 
    dbfile: None | str | PathLike | Connection | Cursor = None, 
    cid: int | str = 0, 
    app: str = "android", 
    *, 
    async_: Literal[True], 
    **request_kwargs, 
) -> Coroutine[Any, Any, int]:
    ...
def updatedb(
    client: str | PathLike | P115Client, 
    dbfile: None | str | PathLike | Connection | Cursor = None, 
    cid: int | str = 0, 
    app: str = "android", 
    *, 
    async_: Literal[False, True] = False, 
    **request_kwargs, 
) -> int | Coroutine[Any, Any, int]:
    """对某个目录执行一次全量拉取，以更新 SQLite 数据

    :param client: 115 网盘客户端对象
    :param dbfile: 数据库文件路径，如果为 None，则自动确定
    :param cid: 目录的 id 或 pickcode
    :param app: 使用指定 app（设备）的接口
    :param async_: 是否异步
    :param request_kwargs: 其它请求参数

    :return: 返回总共影响到数据行数，即所有 DML SQL 执行后，游标的 ``.rowcount`` 累加
    """
    client, con = _init_client(client, dbfile)
    upsert = wrap_async(upsert_items, async_, threaded=True)
    cid = to_id(cid)
    def gen_step():
        start_t = int(time())
        total = 0
        if cid and not has_id(con, cid):
            ancestors = yield get_ancestors_to_cid(
                client, 
                cid, 
                id_to_dirnode=..., 
                app=app, 
                async_=async_, 
                **request_kwargs, 
            )
            if ancestors:
                if ancestors[0]["id"] == 0:
                    ancestors = ancestors[1:]
                if ancestors:
                    total += (yield upsert(con, ancestors, {"is_alive": 1}, commit=True)).rowcount
        with with_iter_next(chunked(
            traverse_tree(
                client, 
                cid, 
                id_to_dirnode=..., 
                app=app, 
                async_=async_, 
                **request_kwargs, 
            ), 
            1000, 
        )) as get_next:
            while True:
                batch = yield get_next()
                total += (yield upsert(con, batch, {"is_alive": 1}, commit=True)).rowcount
        if cid:
            clean_sql = f"""\
UPDATE data SET is_alive = 0 WHERE id in (
    WITH ids(id) AS (
        SELECT id FROM data WHERE parent_id = {cid} AND is_alive AND updated_at < :start_t
        UNION ALL
        SELECT data.id FROM ids JOIN data ON (ids.id = data.parent_id) WHERE is_alive AND updated_at < :start_t
    )
    SELECT id FROM ids
);"""
        else:
            clean_sql = "UPDATE data SET is_alive = 0 WHERE is_alive AND updated_at < :start_t"
        total += (yield wrap_async(execute, async_, threaded=True)(
            con, 
            clean_sql, 
            {"start_t": start_t}, 
            commit=True
        )).rowcount
        return total
    return run_gen_step(gen_step, async_)


@overload
def updatedb_life_iter(
    client: str | PathLike | P115Client, 
    dbfile: None | str | PathLike | Connection | Cursor = None, 
    from_id: int = -1, 
    from_time: float = 0, 
    cooldown: float = 0.2, 
    app: str = "android", 
    *, 
    async_: Literal[False] = False, 
    **request_kwargs, 
) -> Iterator[list[dict]]:
    ...
@overload
def updatedb_life_iter(
    client: str | PathLike | P115Client, 
    dbfile: None | str | PathLike | Connection | Cursor = None, 
    from_id: int = -1, 
    from_time: float = 0, 
    cooldown: float = 0.2, 
    app: str = "android", 
    *, 
    async_: Literal[True], 
    **request_kwargs, 
) -> AsyncIterator[list[dict]]:
    ...
def updatedb_life_iter(
    client: str | PathLike | P115Client, 
    dbfile: None | str | PathLike | Connection | Cursor = None, 
    from_id: int = -1, 
    from_time: float = 0, 
    cooldown: float = 0.2, 
    app: str = "android", 
    *, 
    async_: Literal[False, True] = False, 
    **request_kwargs, 
) -> Iterator[list[dict]] | AsyncIterator[list[dict]]:
    """持续采集 115 生活日志，以更新 SQLite 数据库

    .. note::
        当 ``from_id < 0`` 时，会从数据库获取最大 id 作为 ``from_id``，获取不到时设为 0。
        当 ``from_id != 0`` 时，如果 from_time 为 0，则自动重设为 -1。

    :param client: 115 网盘客户端对象
    :param dbfile: 数据库文件路径，如果为 None，则自动确定
    :param from_id: 开始的事件 id （不含），若 < 0 则是从数据库获取最大 id
    :param from_time: 开始时间（含），若为 0 则从当前时间开始，若 < 0 则从最早开始
    :param cooldown: 冷却时间，大于 0 时，两次接口调用之间至少间隔这么多秒
    :param app: 使用指定 app（设备）的接口
    :param async_: 是否异步
    :param request_kwargs: 其它请求参数

    :return: 迭代器，每次产生一批事件（从当前到上次截止）

    .. code::

        from time import sleep
        from p115client import P115Client
        from p115client.tool import updatedb_life_iter

        client = P115Client.from_path()

        for event_list in updatedb_life_iter(client):
            if event_list:
                print("采集到操作事件列表:", event_list)
            else:
                sleep(1)
    """
    client, con = _init_client(client, dbfile)
    def gen_step():
        nonlocal from_id
        if from_id < 0:
            from_id = yield wrap_async(find, async_, threaded=True)(
                con, 
                "SELECT MAX(id) FROM life", 
                default=0, 
            )
        with with_iter_next(iter_life_behavior_list(
            client, 
            from_id=from_id, 
            from_time=from_time, 
            ignore_types=(10,), 
            cooldown=cooldown,         
            app=app, 
            async_=async_, 
            **request_kwargs, 
        )) as get_next:
            while True:
                event_list = yield get_next()
                event_list.reverse()
                if attrs := list(map(event_normalize_attr, event_list)):
                    if news := [a for a in attrs if a["is_alive"]]:
                        attrs.extend((yield load_missing_ancestors(
                            client, 
                            con, 
                            news, 
                            cooldown=cooldown, 
                            app=app, 
                            async_=async_, 
                            **request_kwargs, 
                        )))
                    yield wrap_async(upsert_items, async_, threaded=True)(
                        con, attrs, commit=True)
                if event_list:
                    yield wrap_async(execute, async_, threaded=True)(
                        con, 
                        "INSERT OR IGNORE INTO life(id, data) VALUES (?, ?)", 
                        [(int(event["id"]), dumps(event)) for event in event_list], 
                        commit=True, 
                    )
                yield Yield(event_list)
    return run_gen_step_iter(gen_step, async_)


@overload
def updatedb_history_iter(
    client: str | PathLike | P115Client, 
    dbfile: None | str | PathLike | Connection | Cursor = None, 
    from_id: int = -1, 
    from_time: float = 0, 
    cooldown: float = 0.2, 
    app: str = "android", 
    *, 
    async_: Literal[False] = False, 
    **request_kwargs, 
) -> Iterator[list[dict]]:
    ...
@overload
def updatedb_history_iter(
    client: str | PathLike | P115Client, 
    dbfile: None | str | PathLike | Connection | Cursor = None, 
    from_id: int = -1, 
    from_time: float = 0, 
    cooldown: float = 0.2, 
    app: str = "android", 
    *, 
    async_: Literal[True], 
    **request_kwargs, 
) -> AsyncIterator[list[dict]]:
    ...
def updatedb_history_iter(
    client: str | PathLike | P115Client, 
    dbfile: None | str | PathLike | Connection | Cursor = None, 
    from_id: int = -1, 
    from_time: float = 0, 
    cooldown: float = 0.2, 
    app: str = "android", 
    *, 
    async_: Literal[False, True] = False, 
    **request_kwargs, 
) -> Iterator[list[dict]] | AsyncIterator[list[dict]]:
    """持续采集 115 历史记录，以更新 SQLite 数据库

    .. note::
        当 ``from_id < 0`` 时，会从数据库获取最大 id 作为 ``from_id``，获取不到时设为 0。
        当 ``from_id != 0`` 时，如果 from_time 为 0，则自动重设为 -1。

    :param client: 115 网盘客户端对象
    :param dbfile: 数据库文件路径，如果为 None，则自动确定
    :param from_id: 开始的事件 id （不含），若 < 0 则是从数据库获取最大 id
    :param from_time: 开始时间（含），若为 0 则从当前时间开始，若 < 0 则从最早开始
    :param cooldown: 冷却时间，大于 0 时，两次接口调用之间至少间隔这么多秒
    :param app: 使用指定 app（设备）的接口
    :param async_: 是否异步
    :param request_kwargs: 其它请求参数

    :return: 迭代器，每次产生一批事件（从当前到上次截止）

    .. code::

        from time import sleep
        from p115client import P115Client
        from p115client.tool import updatedb_history_iter

        client = P115Client.from_path()

        for event_list in updatedb_history_iter(client):
            if event_list:
                print("采集到历史记录列表:", event_list)
            else:
                sleep(1)
    """
    client, con = _init_client(client, dbfile)
    def gen_step():
        nonlocal from_id
        if from_id < 0:
            from_id = yield wrap_async(find, async_, threaded=True)(
                con, 
                "SELECT MAX(id) FROM history", 
                default=0, 
            )
        with with_iter_next(iter_history_list(
            client, 
            from_id=from_id, 
            from_time=from_time, 
            ignore_types=(), 
            cooldown=cooldown,         
            app=app, 
            async_=async_, 
            **request_kwargs, 
        )) as get_next:
            while True:
                event_list = yield get_next()
                event_list.reverse()
                if attrs := list(map(event_normalize_attr, event_list)):
                    if news := [a for a in attrs if a["is_alive"]]:
                        attrs.extend((yield load_missing_ancestors(
                            client, 
                            con, 
                            news, 
                            cooldown=cooldown, 
                            app=app, 
                            async_=async_, 
                            **request_kwargs, 
                        )))
                    yield wrap_async(upsert_items, async_, threaded=True)(
                        con, attrs, commit=True)
                if event_list:
                    yield wrap_async(execute, async_, threaded=True)(
                        con, 
                        "INSERT OR IGNORE INTO history(id, data) VALUES (?, ?)", 
                        [(int(event["id"]), dumps(event)) for event in event_list], 
                        commit=True, 
                    )
                yield Yield(event_list)
    return run_gen_step_iter(gen_step, async_)


class P115QueryDB:
    """封装了一些常用的数据库查询方法，针对 updatedb 产生的 SQLite 数据库

    .. note::
        默认情况下，只有 "id"、"parent_id"、"updated_at" 有索引，所以请自行添加其它需要的索引
    """
    __slots__ = "con",

    def __init__(
        self, 
        /, 
        con: str | PathLike | Connection | Cursor, 
    ):
        if not isinstance(con, (Connection, Cursor)):
            con = connect(
                con, 
                check_same_thread=False, 
                factory=AutoCloseConnection, 
                timeout=inf, 
                uri=isinstance(con, str) and con.startswith("file:"), 
            )
        self.con = con

    def get_ancestors(
        self, 
        id: int = 0, 
        /, 
    ) -> list[dict]:
        """获取某个文件或目录的祖先节点信息，包括 "id"、"parent_id" 和 "name" 字段

        :param self: P115QueryDB 实例或者数据库连接或游标
        :param id: 当前节点的 id

        :return: 当前节点的祖先节点列表，从根目录开始（id 为 0）直到当前节点
        """
        ancestors = [{"id": 0, "parent_id": 0, "name": ""}]
        if not id:
            return ancestors
        con: Any
        if isinstance(self, P115QueryDB):
            con = self.con
        else:
            con = self
        ls = list(query(con, """\
WITH t AS (
    SELECT id, parent_id, name FROM data WHERE id = ?
    UNION ALL
    SELECT data.id, data.parent_id, data.name FROM t JOIN data ON (t.parent_id = data.id)
)
SELECT id, parent_id, name FROM t;""", id))
        if not ls:
            raise FileNotFoundError(errno.ENOENT, id)
        if ls[-1][1]:
            raise ValueError(f"dangling id: {id}")
        ancestors.extend(dict(zip(("id", "parent_id", "name"), record)) for record in reversed(ls))
        return ancestors

    def get_attr(
        self, 
        id: int = 0, 
        /, 
    ) -> dict:
        """获取某个文件或目录的信息

        :param self: P115QueryDB 实例或者数据库连接或游标
        :param id: 当前节点的 id

        :return: 当前节点的信息字典
        """
        if not id:
            return {
                "id": 0, "parent_id": 0, "name": "", "sha1": "", "size": 0, "pickcode": "", 
                "is_dir": 1, "is_alive": 1, "extra": None, "created_at": 0, "updated_at": 0, 
            }
        con: Any
        if isinstance(self, P115QueryDB):
            con = self.con
        else:
            con = self
        return find(
            con, 
            f"SELECT * FROM data WHERE id=? LIMIT 1", 
            id, 
            FileNotFoundError(errno.ENOENT, id), 
            row_factory="dict", 
        )

    def get_id(
        self, 
        /, 
        pickcode: str = "", 
        sha1: str = "", 
        path: str = "", 
        is_alive: bool = True, 
    ) -> int:
        """查询匹配某个字段的文件或目录的 id

        :param self: P115QueryDB 实例或者数据库连接或游标
        :param pickcode: 当前节点的提取码，优先级高于 sha1
        :param sha1: 当前节点的 sha1 校验散列值，优先级高于 path
        :param path: 当前节点的路径

        :return: 当前节点的 id
        """
        con: Any
        if isinstance(self, P115QueryDB):
            con = self.con
        else:
            con = self
        insertion = " AND is_alive" if is_alive else ""
        if pickcode:
            return find(
                con, 
                f"SELECT id FROM data WHERE pickcode=?{insertion} LIMIT 1", 
                pickcode, 
                default=FileNotFoundError(pickcode), 
            )
        elif sha1:
            return find(
                con, 
                f"SELECT id FROM data WHERE sha1=?{insertion} LIMIT 1", 
                sha1, 
                default=FileNotFoundError(sha1), 
            )
        elif path:
            return P115QueryDB.id_to_path(con, path)
        return 0

    def get_parent_id(
        self, 
        id: int = 0, 
        /, 
        default: None | int = None, 
    ) -> int:
        """获取某个 id 对应的父 id

        :param self: P115QueryDB 实例或者数据库连接或游标
        :param id: 当前节点的 id

        :return: 当前节点的父 id
        """
        if id == 0:
            return 0
        con: Any
        if isinstance(self, P115QueryDB):
            con = self.con
        else:
            con = self
        sql = "SELECT parent_id FROM data WHERE id=?"
        return find(con, sql, id, FileNotFoundError(errno.ENOENT, id) if default is None else default)

    def get_path(
        self, 
        id: int = 0, 
        /, 
    ) -> str:
        """获取某个文件或目录的路径

        :param self: P115QueryDB 实例或者数据库连接或游标
        :param id: 当前节点的 id

        :return: 当前节点的路径
        """
        if not id:
            return "/"
        ancestors = P115QueryDB.get_ancestors(self, id)
        return "/".join(escape(a["name"]) for a in ancestors)

    def get_pickcode(
        self, 
        /, 
        id: int = -1, 
        sha1: str = "", 
        path: str = "", 
        is_alive: bool = True, 
    ) -> str:
        """查询匹配某个字段的文件或目录的提取码

        :param self: P115QueryDB 实例或者数据库连接或游标
        :param id: 当前节点的 id，优先级高于 sha1
        :param sha1: 当前节点的 sha1 校验散列值，优先级高于 path
        :param path: 当前节点的路径

        :return: 当前节点的提取码
        """
        con: Any
        if isinstance(self, P115QueryDB):
            con = self.con
        else:
            con = self
        insertion = " AND is_alive" if is_alive else ""
        if id >= 0:
            if not id:
                return ""
            return find(
                con, 
                f"SELECT pickcode FROM data WHERE id=?{insertion} LIMIT 1;", 
                id, 
                default=FileNotFoundError(id), 
            )
        elif sha1:
            return find(
                con, 
                f"SELECT pickcode FROM data WHERE sha1=?{insertion} LIMIT 1;", 
                sha1, 
                default=FileNotFoundError(sha1), 
            )
        else:
            if path in ("", "/"):
                return ""
            return P115QueryDB.get_pickcode(con, P115QueryDB.id_to_path(con, path))

    def get_sha1(
        self, 
        /, 
        id: int = -1, 
        pickcode: str = "", 
        path: str = "", 
        is_alive: bool = True, 
    ) -> str:
        """查询匹配某个字段的文件的 sha1

        :param self: P115QueryDB 实例或者数据库连接或游标
        :param id: 当前节点的 id，优先级高于 pickcode
        :param pickcode: 当前节点的提取码，优先级高于 path
        :param path: 当前节点的路径

        :return: 当前节点的 sha1 校验散列值
        """
        con: Any
        if isinstance(self, P115QueryDB):
            con = self.con
        else:
            con = self
        insertion = " AND is_alive" if is_alive else ""
        if id >= 0:
            if not id:
                return ""
            return find(
                con, 
                f"SELECT sha1 FROM data WHERE id=?{insertion} LIMIT 1;", 
                id, 
                default=FileNotFoundError(id), 
            )
        elif pickcode:
            return find(
                con, 
                f"SELECT sha1 FROM data WHERE pickcode=?{insertion} LIMIT 1;", 
                pickcode, 
                default=FileNotFoundError(pickcode), 
            )
        else:
            if path in ("", "/"):
                return ""
            return P115QueryDB.get_sha1(con, P115QueryDB.id_to_path(con, path))

    def has_id(
        self, 
        id: int, 
        /, 
        is_alive: bool = True, 
    ) -> int:
        """是否存在某个 id

        :param self: P115QueryDB 实例或者数据库连接或游标
        :param id: 当前节点的 id
        :param is_alive: 是否存活

        :return: 如果是 1，则是 True；如果是 0，则是 False
        """
        if id == 0:
            return 1
        elif id < 0:
            return 0
        con: Any
        if isinstance(self, P115QueryDB):
            con = self.con
        else:
            con = self
        sql = "SELECT 1 FROM data WHERE id=?"
        if is_alive:
            sql += " AND is_alive"
        return find(con, sql, id, 0)

    def id_to_path(
        self, 
        /, 
        path: str | Sequence[str] = "", 
        ensure_file: None | bool = None, 
        parent_id: int = 0, 
    ) -> int:
        """查询匹配某个路径的文件或目录的信息字典，只返回找到的第 1 个

        :param self: P115QueryDB 实例或者数据库连接或游标
        :param path: 路径
        :param ensure_file: 是否文件

            - 如果为 True，必须是文件
            - 如果为 False，必须是目录
            - 如果为 None，可以是文件或目录

        :param parent_id: 顶层目录的 id

        :return: 找到的第 1 个匹配的节点 id
        """
        try:
            return next(P115QueryDB.iter_id_to_path(self, path, ensure_file, parent_id))
        except StopIteration:
            raise FileNotFoundError(errno.ENOENT, path) from None

    def iter_count_dir(
        self, 
        parent_id: int = 0, 
        /, 
    ) -> Iterator[dict]:
        """迭代获取所有指定 id 下所有目录节点（包括自己）直属的文件数和目录数

        :param self: P115QueryDB 实例或者数据库连接或游标
        :param parent_id: 顶层目录的 id

        :return: 迭代器，返回字典

            .. code::

                {
                    "id": int, 
                    "parent_id": int, 
                    "dir_count": int, 
                    "file_count": int, 
                }
        """
        con: Any
        if isinstance(self, P115QueryDB):
            con = self.con
        else:
            con = self
        sql = """\
WITH t AS (
    SELECT id, parent_id, is_dir FROM data WHERE parent_id = ? AND is_alive
    UNION ALL
    SELECT data.id, data.parent_id, data.is_dir FROM t JOIN data ON (t.id = data.parent_id) WHERE is_alive
), count AS (
    SELECT parent_id AS id, SUM(is_dir) AS dir_count, SUM(NOT is_dir) AS file_count 
    FROM t 
    GROUP BY parent_id
)
SELECT data.parent_id, count.* FROM count JOIN data USING (id)"""
        return query(con, sql, parent_id, row_factory="dict")

    def iter_count_tree(
        self, 
        parent_id: int = 0, 
        /, 
    ) -> Iterator[dict]:
        """迭代获取所有指定 id 下所有目录节点（包括自己）直属的文件数和目录数，以及子树下的文件数合计和目录🌲合计

        :param self: P115QueryDB 实例或者数据库连接或游标
        :param parent_id: 顶层目录的 id

        :return: 迭代器，返回字典

            .. code::

                {
                    "id": int, 
                    "parent_id": int, 
                    "dir_count": int, 
                    "file_count": int, 
                    "tree_dir_count": int, 
                    "tree_file_count": int, 
                }
        """
        from iter_collect import grouped_mapping
        data = {a["id"]: a for a in P115QueryDB.iter_count_dir(self, parent_id)}
        id_to_children = grouped_mapping((a["parent_id"], id) for id, a in data.items())
        def calc(attr: dict, /) -> dict:
            if children := id_to_children.get(attr["id"]):
                for cid in children:
                    cattr = data[cid]
                    if "tree_dir_count" not in cattr:
                        calc(cattr)
                    attr["tree_dir_count"] = attr.get("tree_dir_count", 0) + 1 + cattr["tree_dir_count"]
                    attr["tree_file_count"] = attr.get("tree_file_count", attr["file_count"]) + cattr["tree_file_count"]
            elif "tree_dir_count" not in attr:
                attr["tree_dir_count"] = attr["dir_count"]
                attr["tree_file_count"] = attr["file_count"]
            return attr
        return map(calc, data.values())

    def iter_children(
        self, 
        parent_id: int | dict = 0, 
        /, 
        ensure_file: None | bool = None, 
    ) -> Iterator[dict]:
        """获取某个目录之下的文件或目录的信息

        :param self: P115QueryDB 实例或者数据库连接或游标
        :param parent_id: 父目录的 id
        :param ensure_file: 是否仅输出文件

            - 如果为 True，仅输出文件
            - 如果为 False，仅输出目录
            - 如果为 None，全部输出

        :return: 迭代器，产生一组信息的字典
        """
        con: Any
        if isinstance(self, P115QueryDB):
            con = self.con
        else:
            con = self
        if isinstance(parent_id, int):
            attr = P115QueryDB.get_attr(con, parent_id)
        else:
            attr = parent_id
        if not attr["is_dir"]:
            raise NotADirectoryError(errno.ENOTDIR, attr)
        sql = "SELECT * FROM data WHERE parent_id=? AND is_alive"
        if ensure_file is not None:
            if ensure_file:
                sql += " AND NOT is_dir"
            else:
                sql += " AND is_dir"
        return query(con, sql, attr["id"], row_factory="dict")

    def iter_dangling_ids(
        self, 
        /, 
    ) -> Iterator[int]:
        """罗列所有悬空的文件或目录的 id

        .. note::
            悬空的 id，即祖先节点中，存在一个节点，它的 parent_id 是悬空的

        :param self: P115QueryDB 实例或者数据库连接或游标

        :return: 迭代器，一组目录的 id
        """
        con: Any
        if isinstance(self, P115QueryDB):
            con = self.con
        else:
            con = self
        sql = """\
WITH dangling_ids(id) AS (
    SELECT d1.id
    FROM data AS d1 LEFT JOIN data AS d2 ON (d1.parent_id = d2.id)
    WHERE d1.parent_id AND d2.id IS NULL
    UNION ALL
    SELECT data.id FROM dangling_ids JOIN data ON (dangling_ids.id = data.parent_id)
)
SELECT id FROM dangling_ids"""
        return query(con, sql, row_factory="one")

    def iter_dangling_parent_ids(
        self, 
        /, 
    ) -> Iterator[int]:
        """罗列所有悬空的 parent_id

        .. note::
            悬空的 parent_id，即所有的 parent_id 中，，不为 0 且不在 `data` 表中的部分

        :param self: P115QueryDB 实例或者数据库连接或游标

        :return: 迭代器，一组目录的 id
        """
        con: Any
        if isinstance(self, P115QueryDB):
            con = self.con
        else:
            con = self
        sql = """\
SELECT
    DISTINCT d1.parent_id
FROM
    data AS d1 LEFT JOIN data AS d2 ON (d1.parent_id = d2.id)
WHERE
    d1.parent_id AND d2.id IS NULL"""
        return query(con, sql, row_factory="one")

    def iter_descendants(
        self, 
        parent_id: int | dict = 0, 
        /, 
        min_depth: int = 1, 
        max_depth: int = -1, 
        ensure_file: None | bool = None, 
        use_relpath: None | bool = False, 
        with_root: bool = False, 
        topdown: None | bool = True, 
    ) -> Iterator[dict]:
        """遍历获取某个目录之下的所有文件或目录的信息

        :param self: P115QueryDB 实例或者数据库连接或游标
        :param parent_id: 顶层目录的 id
        :param min_depth: 最小深度
        :param max_depth: 最大深度。如果小于 0，则无限深度
        :param ensure_file: 是否仅输出文件

            - 如果为 True，仅输出文件
            - 如果为 False，仅输出目录
            - 如果为 None，全部输出

        :param use_relpath: 是否仅输出相对路径。如果为 False，则输出完整路径（从 / 开始）；如果为 None，则不输出 "ancestors", "path", "posixpath"
        :param with_root: 仅当 `use_relpath=True` 时生效。如果为 True，则相对路径包含 `parent_id` 对应的节点
        :param topdown: 是否自顶向下深度优先遍历

            - 如果为 True，则自顶向下深度优先遍历
            - 如果为 False，则自底向上深度优先遍历
            - 如果为 None，则自顶向下宽度优先遍历

        :return: 迭代器，产生一组信息的字典，包含如下字段：

            .. code:: python

                (
                    "id", "parent_id", "pickcode", "sha1", "name", "size", "is_dir", 
                    "type", "is_alive", "extra", "created_at", "updated_at", 
                    "depth", "ancestors", "path", "posixpath", 
                )
        """
        con: Any
        if isinstance(self, P115QueryDB):
            con = self.con
        else:
            con = self
        with_path = use_relpath is not None
        if isinstance(parent_id, int):
            if 0 <= max_depth < min_depth:
                return
            depth = 1
            if with_path:
                if not parent_id:
                    ancestors: list[dict] = [{"id": 0, "parent_id": 0, "name": ""}]
                    dir_ = posixdir = "/"
                elif use_relpath:
                    if with_root:
                        attr = parent_id = P115QueryDB.get_attr(con, parent_id)
                        name = attr["name"]
                        ancestors = [{"id": attr["id"], "parent_id": attr["parent_id"], "name": name}]
                        dir_ = escape(name) + "/"
                        posixdir = name.replace("/", "|") + "/"
                    else:
                        ancestors = []
                        dir_ = posixdir = ""
                else:
                    ancestors = P115QueryDB.get_ancestors(con, parent_id)
                    dir_ = "/".join(escape(a["name"]) for a in ancestors) + "/"
                    posixdir = "/".join(a["name"].replace("/", "|") for a in ancestors) + "/"
        else:
            attr = parent_id
            depth = attr["depth"] + 1
            if with_path:
                ancestors = attr["ancestors"]
                dir_ = attr["path"]
                posixdir = attr["posixpath"]
                if dir_ != "/":
                    dir_ += "/"
                    posixdir += "/"
        if topdown is None:
            if with_path:
                gen = bfs_gen((parent_id, 0, ancestors, dir_, posixdir))
            else:
                gen = bfs_gen((parent_id, 0)) # type: ignore
            send: Callable = gen.send
            p: list
            for parent_id, depth, *p in gen:
                depth += 1
                will_step_in = max_depth < 0 or depth < max_depth
                will_yield = min_depth <= depth and (max_depth < 0 or depth <= max_depth)
                if with_path:
                    ancestors, dir_, posixdir = p
                for attr in P115QueryDB.iter_children(con, parent_id, False if ensure_file is False else None):
                    attr["depth"] = depth
                    is_dir = attr["is_dir"]
                    if with_path:
                        attr["ancestors"] = [
                            *ancestors, 
                            {k: attr[k] for k in ("id", "parent_id", "name")}, 
                        ]
                        attr["path"] = dir_ + escape(attr["name"])
                        attr["posixpath"] = posixdir + attr["name"].replace("/", "|")
                    if is_dir and will_step_in:
                        if with_path:
                            send((attr, depth, attr["ancestors"], attr["path"] + "/", attr["posixpath"] + "/"))
                        else:
                            send((attr, depth))
                    if will_yield:
                        if ensure_file is None:
                            yield attr
                        elif is_dir:
                            if not ensure_file:
                                yield attr
                        elif ensure_file:
                            yield attr
        else:
            will_step_in = max_depth < 0 or depth < max_depth
            will_yield = min_depth <= depth and (max_depth < 0 or depth <= max_depth)
            for attr in P115QueryDB.iter_children(con, parent_id, False if ensure_file is False else None):
                is_dir = attr["is_dir"]
                attr["depth"] = depth
                if with_path:
                    attr["ancestors"] = [
                        *ancestors, 
                        {k: attr[k] for k in ("id", "parent_id", "name")}, 
                    ]
                    attr["path"] = dir_ + escape(attr["name"])
                    attr["posixpath"] = posixdir + attr["name"].replace("/", "|")
                if will_yield and topdown:
                    if ensure_file is None:
                        yield attr
                    elif is_dir:
                        if not ensure_file:
                            yield attr
                    elif ensure_file:
                        yield attr
                if is_dir and will_step_in:
                    yield from P115QueryDB.iter_descendants(
                        con, 
                        attr, 
                        min_depth=min_depth, 
                        max_depth=max_depth, 
                        ensure_file=ensure_file, 
                        use_relpath=use_relpath, 
                        with_root=with_root, 
                        topdown=topdown, 
                    )
                if will_yield and not topdown:
                    if ensure_file is None:
                        yield attr
                    elif is_dir:
                        if not ensure_file:
                            yield attr
                    elif ensure_file:
                        yield attr

    @overload
    def iter_descendants_bfs(
        self, 
        parent_id: int = 0, 
        /, 
        min_depth: int = 1, 
        max_depth: int = -1, 
        ensure_file: None | bool = None, 
        use_relpath: bool = False, 
        with_root: bool = False, 
        *, 
        fields: str, 
    ) -> Iterator[Any]:
        ...
    @overload
    def iter_descendants_bfs(
        self, 
        parent_id: int = 0, 
        /, 
        min_depth: int = 1, 
        max_depth: int = -1, 
        ensure_file: None | bool = None, 
        use_relpath: bool = False, 
        with_root: bool = False, 
        *, 
        fields: tuple[str, ...] = (
            "id", "parent_id", "name", "sha1", "size", "pickcode", 
            "is_dir", "is_alive", "extra", "created_at", "updated_at", 
            "depth", "ancestors", "path", "posixpath", 
        ), 
        to_dict: Literal[False], 
    ) -> Iterator[tuple[Any, ...]]:
        ...
    @overload
    def iter_descendants_bfs(
        self, 
        parent_id: int = 0, 
        /, 
        min_depth: int = 1, 
        max_depth: int = -1, 
        ensure_file: None | bool = None, 
        use_relpath: bool = False, 
        with_root: bool = False, 
        *, 
        fields: tuple[str, ...] = (
            "id", "parent_id", "name", "sha1", "size", "pickcode", 
            "is_dir", "is_alive", "extra", "created_at", "updated_at", 
            "depth", "ancestors", "path", "posixpath", 
        ), 
        to_dict: Literal[True] = True, 
    ) -> Iterator[dict[str, Any]]:
        ...
    def iter_descendants_bfs(
        self, 
        parent_id: int = 0, 
        /, 
        min_depth: int = 1, 
        max_depth: int = -1, 
        ensure_file: None | bool = None, 
        use_relpath: bool = False, 
        with_root: bool = False, 
        *, 
        fields: str | tuple[str, ...] = (
            "id", "parent_id", "name", "sha1", "size", "pickcode", 
            "is_dir", "is_alive", "extra", "created_at", "updated_at", 
            "depth", "ancestors", "path", "posixpath", 
        ), 
        to_dict: bool = True, 
    ) -> Iterator:
        """获取某个目录之下的所有目录节点的 id 或者信息字典（宽度优先遍历）

        :param self: P115QueryDB 实例或者数据库连接或游标
        :param parent_id: 顶层目录的 id
        :param min_depth: 最小深度
        :param max_depth: 最大深度。如果小于 0，则无限深度
        :param ensure_file: 是否仅输出文件

            - 如果为 True，仅输出文件
            - 如果为 False，仅输出目录
            - 如果为 None，全部输出

        :param use_relpath: 仅输出相对路径，否则输出完整路径（从 / 开始）
        :param with_root: 仅当 `use_relpath=True` 时生效。如果为 True，则相对路径包含 `parent_id` 对应的节点
        :param fields: 需要获取的字段

            - 如果为 str，则获取指定的字段的值
            - 如果为 tuple，则拉取这一组字段的值

        :param to_dict: 是否产生字典，如果为 True 且 fields 不为 str，则产生字典

        :return: 迭代器，产生一组数据
        """
        con: Any
        if isinstance(self, P115QueryDB):
            con = self.con
        else:
            con = self
        extended_fields = frozenset(("depth", "ancestors", "path", "posixpath"))
        one_value = False
        parse: None | Callable = None
        if isinstance(fields, str):
            one_value = True
            field = fields
            with_depth = max_depth > 1 or "depth" == field
            with_ancestors = "ancestors" == field
            with_path = "path" == field
            with_posixpath = "posixpath" == field
            fields = field,
        else:
            with_depth = max_depth > 1 or min_depth > 1 or "depth" in fields
            with_ancestors = "ancestors" in fields
            with_path = "path" in fields
            with_posixpath = "posixpath" in fields
        select_fields1 = ["id"]
        select_fields1.extend(set(fields) - {"id"} - extended_fields)
        select_fields2 = ["data." + f for f in select_fields1]
        where1, where2 = "", ""
        if with_depth:
            select_fields1.append("1 AS depth")
            select_fields2.append("t.depth + 1")
        if with_path or with_posixpath or with_ancestors:
            if not parent_id:
                ancestors: list[dict] = [{"id": 0, "parent_id": 0, "name": ""}]
                path = posixpath = "/"
            elif use_relpath:
                if with_root:
                    attr = P115QueryDB.get_attr(con, parent_id)
                    name = attr["name"]
                    ancestors = [{"id": attr["id"], "parent_id": attr["parent_id"], "name": name}]
                    path = escape(name) + "/"
                    posixpath = name.replace("/", "|") + "/"
                else:
                    ancestors = []
                    path = posixpath = ""
            else:
                ancestors = P115QueryDB.get_ancestors(con, parent_id)
                if with_path:
                    path = "/".join(escape(a["name"]) for a in ancestors) + "/"
                if with_posixpath:
                    posixpath = "/".join(a["name"].replace("/", "|") for a in ancestors) + "/"
            if with_ancestors:
                if ancestors:
                    parse_ancestors = lambda val: [*ancestors, *loads("[%s]" % val)]
                else:
                    parse_ancestors = lambda val: loads("[%s]" % val)
                parse = parse_ancestors
                select_fields1.append("json_object('id', id, 'parent_id', parent_id, 'name', name) AS ancestors")
                select_fields2.append("concat(t.ancestors, ',', json_object('id', data.id, 'parent_id', data.parent_id, 'name', data.name))")
            if with_path:
                def parse_path(val: str, /) -> str:
                    return path + val
                parse = parse_path
                if isinstance(con, Cursor):
                    conn = con.connection
                else:
                    conn = con
                conn.create_function("escape_name", 1, escape, deterministic=True)
                select_fields1.append("escape_name(name) AS path")
                select_fields2.append("concat(t.path, '/', escape_name(data.name))")
            if with_posixpath:
                def parse_posixpath(val: str, /) -> str:
                    return posixpath + val
                parse = parse_posixpath
                select_fields1.append("replace(name, '/', '|') AS posixpath")
                select_fields2.append("concat(t.posixpath, '/', replace(data.name, '/', '|'))")
        if min_depth <= 1 and max_depth in (0, 1) or 0 <= max_depth < min_depth:
            if 0 <= max_depth < min_depth:
                where1 = " AND FALSE"
            elif ensure_file:
                where1 = " AND NOT is_dir"
            elif ensure_file is False:
                where1 = " AND is_dir"
            sql = f"""\
    WITH t AS (
        SELECT {",".join(select_fields1)} FROM data WHERE parent_id={parent_id:d} AND is_alive{where1}
    ) SELECT {",".join(fields)} FROM t"""
        else:
            if max_depth > 1:
                where2 = f" AND depth < {max_depth:d}"
            if ensure_file:
                if "is_dir" not in fields:
                    select_fields1.append("is_dir")
                    select_fields2.append("data.is_dir")
            elif ensure_file is False:
                where1 += " AND is_dir"
                where2 += " AND data.is_dir"
            sql = f"""\
    WITH t AS (
        SELECT {",".join(select_fields1)} FROM data WHERE parent_id={parent_id:d} AND is_alive{where1}
        UNION ALL
        SELECT {",".join(select_fields2)} FROM t JOIN data ON(t.id = data.parent_id) WHERE data.is_alive{where2}
    ) SELECT {",".join(fields)} FROM t WHERE True"""
            if ensure_file:
                sql += " AND NOT is_dir"
            if min_depth > 1:
                sql += f" AND depth >= {min_depth:d}"
        if one_value:
            if parse is None:
                row_factory = lambda _, r: r[0]
            else:
                row_factory = lambda _, r: parse(r[0])
        elif to_dict:
            def row_factory(_, r):
                d = dict(zip(fields, r))
                if with_ancestors:
                    d["ancestors"] = parse_ancestors(d["ancestors"])
                if with_path:
                    d["path"] = parse_path(d["path"])
                if with_posixpath:
                    d["posixpath"] = parse_posixpath(d["posixpath"])
                return d
        else:
            with_route = with_ancestors or with_path or with_posixpath
            def parse(f, v):
                match f:
                    case "ancestors":
                        return parse_ancestors(v)
                    case "path":
                        return parse_path(v)
                    case "posixpath":
                        return parse_posixpath(v)
                    case _:
                        return v
            def row_factory(_, r):
                if with_route:
                    return tuple(parse(f, v) for f, v in zip(fields, r))
                return r
        return query(con, sql, row_factory=row_factory)

    def iter_dup_files(
        self, 
        /, 
    ) -> Iterator[dict]:
        """罗列所有重复文件

        :param self: P115QueryDB 实例或者数据库连接或游标

        :return: 迭代器，一组文件的信息
        """
        con: Any
        if isinstance(self, P115QueryDB):
            con = self.con
        else:
            con = self
        sql = f"""\
WITH stats AS (
    SELECT
        COUNT(1) OVER w AS total, 
        ROW_NUMBER() OVER w AS nth, 
        *
    FROM data
    WHERE NOT is_dir AND is_alive
    WINDOW w AS (PARTITION BY sha1, size)
)
SELECT * FROM stats WHERE total > 1"""
        return query(con, sql, row_factory="dict")

    def iter_existing_id(
        self, 
        ids: Iterable[int], 
        /, 
        is_alive: bool = True, 
    ) -> Iterator[int]:
        """筛选出一系列 id 中，在数据库中存在的

        :param self: P115QueryDB 实例或者数据库连接或游标
        :param ids: 一系列的节点 id
        :param is_alive: 是否存活

        :return: 一系列 id 中在数据库中存在的那些的迭代器（实际直接返回一个游标）
        """
        con: Any
        if isinstance(self, P115QueryDB):
            con = self.con
        else:
            con = self
        sql = "SELECT id FROM data WHERE id IN (%s)" % (",".join(map("%d".__mod__, ids)) or "NULL")
        if is_alive:
            sql += " AND is_alive"
        return query(con, sql, row_factory="one")

    def iter_files_with_path_url(
        self, 
        parent_id: int = 0, 
        /, 
        base_url: str = "http://localhost:8000", 
    ) -> Iterator[tuple[str, str]]:
        """迭代获取所有文件的路径和下载链接

        :param self: P115QueryDB 实例或者数据库连接或游标
        :param parent_id: 顶层目录的 id
        :param base_url: 115 的 302 服务后端地址

        :return: 迭代器，返回每个文件的 路径 和 下载链接 的 2 元组
        """
        from encode_uri import encode_uri_component_loose
        con: Any
        if isinstance(self, P115QueryDB):
            con = self.con
        else:
            con = self
        code = compile('f"%s/{quote(name, '"''"')}?{id=}&{pickcode=!s}&{sha1=!s}&{size=}&file=true"' % base_url.translate({ord(c): c*2 for c in "{}"}), "-", "eval")
        for attr in P115QueryDB.iter_descendants_bfs(
            con, 
            parent_id, 
            fields=("id", "sha1", "pickcode", "size", "name", "posixpath"), 
            ensure_file=True, 
        ):
            yield attr["posixpath"], eval(code, {"quote": encode_uri_component_loose}, attr)

    def iter_id_to_parent_id(
        self, 
        ids: Iterable[int], 
        /, 
        recursive: bool = False, 
    ) -> Iterator[tuple[int, int]]:
        """找出一系列 id 所对应的父 id，返回 ``(id, parent_id)`` 的 2 元组

        :param self: P115QueryDB 实例或者数据库连接或游标
        :param ids: 一系列的节点 id
        :param recursive: 是否递归，如果为 True，则还会处理它们的祖先节点

        :return: 迭代器，产生 ``(id, parent_id)`` 的 2 元组（实际直接返回一个游标）
        """
        con: Any
        if isinstance(self, P115QueryDB):
            con = self.con
        else:
            con = self
        s_ids = "(%s)" % (",".join(map(str, ids)) or "NULL")
        if recursive:
            sql = """\
WITH pairs AS (
    SELECT id, parent_id FROM data WHERE id IN %s
    UNION ALL
    SELECT data.id, data.parent_id FROM pairs JOIN data ON (pairs.parent_id = data.id)
) SELECT * FROM pairs""" % s_ids
        else:
            sql = "SELECT id, parent_id FROM data WHERE id IN %s" % s_ids
        return query(con, sql)

    def iter_id_to_path(
        self, 
        /, 
        path: str | Sequence[str] = "", 
        ensure_file: None | bool = None, 
        parent_id: int = 0, 
    ) -> Iterator[int]:
        """查询匹配某个路径的文件或目录的信息字典

        .. note::
            同一个路径可以有多条对应的数据

        :param self: P115QueryDB 实例或者数据库连接或游标
        :param path: 路径
        :param ensure_file: 是否文件

            - 如果为 True，必须是文件
            - 如果为 False，必须是目录
            - 如果为 None，可以是文件或目录

        :param parent_id: 顶层目录的 id

        :return: 迭代器，产生一组匹配指定路径的（文件或目录）节点的 id
        """
        con: Any
        if isinstance(self, P115QueryDB):
            con = self.con
        else:
            con = self
        patht: Sequence[str]
        if isinstance(path, str):
            if ensure_file is None and path_is_dir_form(path):
                ensure_file = False
            patht, _ = splits("/" + path)
        else:
            patht = ("", *filter(None, path))
        if not parent_id and len(patht) == 1:
            return iter((0,))
        if len(patht) > 2:
            sql = "SELECT id FROM data WHERE parent_id=? AND name=? AND is_alive AND is_dir LIMIT 1"
            for name in patht[1:-1]:
                parent_id = find(con, sql, (parent_id, name), default=-1)
                if parent_id < 0:
                    return iter(())
        sql = "SELECT id FROM data WHERE parent_id=? AND name=? AND is_alive"
        if ensure_file is None:
            sql += " ORDER BY is_dir DESC"
        elif ensure_file:
            sql += " AND NOT is_dir"
        else:
            sql += " AND is_dir LIMIT 1"
        return query(con, sql, (parent_id, patht[-1]), row_factory="one")

    def iter_parent_id(
        self, 
        ids: Iterable[int], 
        /, 
    ) -> Iterator[int]:
        """找出一系列 id 所对应的父 id

        :param self: P115QueryDB 实例或者数据库连接或游标
        :param ids: 一系列的节点 id

        :return: 它们的父 id 的迭代器（实际直接返回一个游标）
        """
        con: Any
        if isinstance(self, P115QueryDB):
            con = self.con
        else:
            con = self
        sql = "SELECT parent_id FROM data WHERE id IN (%s)" % (",".join(map("%d".__mod__, ids)) or "NULL")
        return query(con, sql, row_factory="one")

    def select_na_ids(
        self, 
        /, 
    ) -> set[int]:
        """找出所有的失效节点和悬空节点的 id

        .. note::
            悬空节点，就是此节点有一个祖先节点的 parant_id，不为 0 且不在 `data` 表中

        :param self: P115QueryDB 实例或者数据库连接或游标

        :return: 一组悬空节点的 id 的集合
        """
        con: Any
        if isinstance(self, P115QueryDB):
            con = self.con
        else:
            con = self
        ok_ids: set[int] = set(query(con, "SELECT id FROM data WHERE NOT is_alive", row_factory="one"))
        na_ids: set[int] = set()
        d = dict(query(con, "SELECT id, parent_id FROM data WHERE is_alive"))
        temp: list[int] = []
        push = temp.append
        clear = temp.clear
        update_ok = ok_ids.update
        update_na = na_ids.update
        for k in d:
            try:
                push(k)
                while k := d[k]:
                    if k in ok_ids:
                        update_ok(temp)
                        break
                    elif k in na_ids:
                        update_na(temp)
                        break
                    push(k)
                else:
                    update_ok(temp)
            except KeyError:
                update_na(temp)
            finally:
                clear()
        return na_ids

    def dump_to_alist(
        self, 
        /, 
        alist_db: str | PathLike | Connection | Cursor = expanduser("~/alist.d/data/data.db"), 
        parent_id: int = 0, 
        dirname: str = "/115", 
        clean: bool = True, 
    ) -> int:
        """把 p115updatedb 导出的数据，导入到 alist 的搜索索引

        :param self: P115QueryDB 实例或者数据库连接或游标
        :param alist_db: alist 数据库文件路径或连接
        :param parent_id: 在 p115updatedb 所导出数据库中的顶层目录 id
        :param dirname: 在 alist 中所对应的的顶层目录路径
        :param clean: 在插入前先清除 alist 的数据库中 `dirname` 目录下的所有数据

        :return: 总共导入的数量
        """
        con: Any
        if isinstance(self, P115QueryDB):
            con = self.con
        else:
            con = self
        sql = """\
WITH t AS (
    SELECT 
        :dirname AS parent, 
        name, 
        is_dir, 
        size, 
        id, 
        CASE WHEN is_dir THEN CONCAT(:dirname, '/', REPLACE(name, '/', '|')) END AS dirname 
    FROM data WHERE parent_id=:parent_id AND is_alive
    UNION ALL
    SELECT 
        t.dirname AS parent, 
        data.name, 
        data.is_dir, 
        data.size, 
        data.id, 
        CASE WHEN data.is_dir THEN CONCAT(t.dirname, '/', REPLACE(data.name, '/', '|')) END AS dirname
    FROM t JOIN data ON(t.id = data.parent_id) WHERE data.is_alive
)
SELECT parent, name, is_dir, size FROM t"""
        dirname = "/" + dirname.strip("/")
        with transact(alist_db) as cur:
            if clean:
                cur.execute("DELETE FROM x_search_nodes WHERE parent=? OR parent LIKE ? || '/%';", (dirname, dirname))
            count = 0
            executemany = cur.executemany
            for items in batched(query(con, sql, {"parent_id": parent_id, "dirname": dirname}), 10_000):
                executemany("INSERT INTO x_search_nodes(parent, name, is_dir, size) VALUES (?, ?, ?, ?)", items)
                count += len(items)
            return count

    def dump_efu(
        self, 
        /, 
        efu_file: str | PathLike = "export.efu", 
        parent_id: int = 0, 
        dirname: str = "", 
        use_relpath: bool = False, 
    ) -> int:
        """把 p115updatedb 导出的数据，导出为 efu 文件，可供 everything 软件使用

        :param self: P115QueryDB 实例或者数据库连接或游标
        :param efu_file: 要导出的文件路径
        :param parent_id: 在 p115updatedb 所导出数据库中的顶层目录 id
        :param dirname: 给每个导出路径添加的目录前缀
        :param use_relpath: 是否使用相对路径

        :return: 总共导出的数量
        """
        con: Any
        if isinstance(self, P115QueryDB):
            con = self.con
        else:
            con = self
        def unix_to_filetime(unix_time: float, /) -> int:
            return int(unix_time * 10 ** 7) + 11644473600 * 10 ** 7
        if dirname:
            dirname = normpath(dirname)
            if not dirname.endswith("\\"):
                dirname += "\\"
        n = 0
        with open(efu_file, "w", newline="", encoding="utf-8") as file:
            csvfile = writer(file)
            writerow = csvfile.writerow
            writerow(("Filename", "Size", "Date Modified", "Date Created", "Attributes"))
            for n, (size, ctime, mtime, is_dir, path) in enumerate(P115QueryDB.iter_descendants_bfs(
                con, 
                parent_id, 
                use_relpath=use_relpath, 
                fields=("size", "created_at", "updated_at", "is_dir", "posixpath"), 
                to_dict=False, 
            ), 1):
                if use_relpath:
                    path = normpath(path)
                else:
                    path = normpath(path[1:])
                writerow((
                    dirname + path, 
                    size, 
                    unix_to_filetime(mtime), 
                    unix_to_filetime(ctime), 
                    16 if is_dir else 0, 
                ))
        return n

