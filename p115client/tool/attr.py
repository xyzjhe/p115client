#!/usr/bin/env python3
# encoding: utf-8

__author__ = "ChenyangGao <https://chenyanggao.github.io>"
__all__ = [
    "type_of_attr", "get_attr", "get_info", "iter_list", 
    "get_ancestors", "get_path", "get_id", "get_id_to_path", 
    "get_id_to_sha1", "get_id_to_name", "share_get_id", 
    "share_get_id_to_path", "share_get_id_to_name", "get_file_count", 
]
__doc__ = "这个模块提供了一些和文件或目录信息有关的函数"

from collections.abc import (
    AsyncIterator, Callable, Coroutine, Iterator, Mapping, MutableMapping, 
    Sequence, 
)
from functools import partial
from itertools import cycle, dropwhile
from operator import attrgetter
from os import PathLike
from types import EllipsisType
from typing import cast, overload, Any, Final, Literal

from dicttools import get_first
from errno2 import errno
from iterutils import run_gen_step, run_gen_step_iter, with_iter_next, Yield
from p115client import (
    check_response, normalize_attr, normalize_attr_web, 
    P115Client, P115OpenClient, 
)
from p115client.const import CLASS_TO_TYPE, SUFFIX_TO_TYPE, ID_TO_DIRNODE_CACHE
from p115client.exception import P115FileNotFoundError
from p115client.type import P115ID
from p115pickcode import to_id
from posixpatht import path_is_dir_form, splitext, splits

from .fs_files import iter_fs_files_serialized
from .iterdir import overview_attr, iterdir, share_iterdir, update_resp_ancestors
from .util import (
    posix_escape_name, share_extract_payload, unescape_115_charref, 
    is_valid_id, is_valid_sha1, is_valid_name, is_valid_pickcode, 
)


get_webapi_origin: Final = cycle(("http://web.api.115.com", "https://webapi.115.com")).__next__
get_proapi_origin: Final = cycle(("http://pro.api.115.com", "https://proapi.115.com")).__next__


def type_of_attr(attr: str | Mapping, /) -> int:
    """推断文件信息所属类型（试验版，未必准确）

    .. note::
        如果直接传入文件名，则视为文件，在获取不到时，返回 99（如果你已知这是目录，你直接自己就能计作 0）

    :param attr: 文件名或文件信息

    :return: 返回类型代码

        - 0: 目录
        - 1: 文档
        - 2: 图片
        - 3: 音频
        - 4: 视频
        - 5: 压缩包
        - 6: 应用
        - 7: 书籍
        - 99: 其它文件
"""
    if not attr:
        return 0
    if isinstance(attr, str):
        suffix = splitext(attr)[1]
        if not suffix:
            return 99
        return SUFFIX_TO_TYPE.get(suffix, 99)
    if attr.get("is_dir"):
        return 0
    type: None | int
    if type := CLASS_TO_TYPE.get(attr.get("class", "")):
        return type
    if type := SUFFIX_TO_TYPE.get(splitext(attr["name"])[1].lower()):
        return type
    if attr.get("is_video") or "defination" in attr:
        return 4
    return 99


@overload
def get_attr(
    client: str | PathLike | P115Client, 
    id: int | str = 0, 
    skim: bool = False, 
    *, 
    async_: Literal[False] = False, 
    **request_kwargs, 
) -> dict:
    ...
@overload
def get_attr(
    client: str | PathLike | P115Client, 
    id: int | str = 0, 
    skim: bool = False, 
    *, 
    async_: Literal[True], 
    **request_kwargs, 
) -> Coroutine[Any, Any, dict]:
    ...
def get_attr(
    client: str | PathLike | P115Client, 
    id: int | str = 0, 
    skim: bool = False, 
    *, 
    async_: Literal[False, True] = False, 
    **request_kwargs, 
) -> dict | Coroutine[Any, Any, dict]:
    """获取文件或目录的信息

    :param client: 115 客户端或 cookies
    :param id: 文件或目录的 id 或 pickcode
    :param skim: 是否获取简要信息（可避免风控）
    :param async_: 是否异步
    :param request_kwargs: 其它请求参数

    :return: 文件或目录的信息
    """
    if isinstance(client, (str, PathLike)):
        client = P115Client(client, check_for_relogin=True)
    id = to_id(id)
    def gen_step():
        from dictattr import AttrDict
        if skim:
            if not id:
                return {
                    "id": 0, 
                    "name": "", 
                    "pickcode": "", 
                    "sha1": "", 
                    "size": 0, 
                    "is_dir": True, 
                }
            resp = yield client.fs_file_skim(id, async_=async_, **request_kwargs)
            check_response(resp)
            info = resp["data"][0]
            return AttrDict(
                id=int(info["file_id"]), 
                name=unescape_115_charref(info["file_name"]), 
                pickcode=info["pick_code"], 
                sha1=info["sha1"], 
                size=int(info["file_size"]), 
                is_dir=not info["sha1"], 
            )
        else:
            if not id:
                return {
                    "is_dir": True,
                    "id": 0, 
                    "parent_id": 0, 
                    "name": "", 
                    "sha1": "", 
                    "size": 0, 
                    "pickcode": "", 
                    "pick_code": "", 
                    "ico": "folder", 
                    "mtime": 0, 
                    "user_utime": 0, 
                    "ctime": 0, 
                    "user_ptime": 0, 
                    "atime": 0, 
                    "user_otime": 0, 
                    "utime": 0, 
                    "time": 0, 
                    "has_desc": 0, 
                    "area_id": 1, 
                    "cover": "", 
                    "category_cover": "", 
                    "pick_expire": "", 
                    "labels": [], 
                    "is_private": 0, 
                    "is_top": 0, 
                    "show_play_long": 0, 
                    "is_shortcut": 0, 
                    "is_mark": 0, 
                    "star": 0, 
                    "score": 0, 
                    "is_share": 0, 
                    "thumb": "", 
                    "type": 0, 
                }
            resp = yield client.fs_file(id, async_=async_, **request_kwargs)
            check_response(resp)
            return normalize_attr_web(resp["data"][0], dict_cls=AttrDict)
    return run_gen_step(gen_step, async_)


@overload
def get_info(
    client: str | PathLike | P115Client | P115OpenClient, 
    id: int | str = 0, 
    id_to_dirnode: None | EllipsisType | MutableMapping[int, tuple[str, int]] = None, 
    app: str = "web", 
    *, 
    async_: Literal[False] = False, 
    **request_kwargs, 
) -> dict:
    ...
@overload
def get_info(
    client: str | PathLike | P115Client | P115OpenClient, 
    id: int | str = 0, 
    id_to_dirnode: None | EllipsisType | MutableMapping[int, tuple[str, int]] = None, 
    app: str = "web", 
    *, 
    async_: Literal[True], 
    **request_kwargs, 
) -> Coroutine[Any, Any, dict]:
    ...
def get_info(
    client: str | PathLike | P115Client | P115OpenClient, 
    id: int | str = 0, 
    id_to_dirnode: None | EllipsisType | MutableMapping[int, tuple[str, int]] = None, 
    app: str = "web", 
    *, 
    async_: Literal[False, True] = False, 
    **request_kwargs, 
) -> dict | Coroutine[Any, Any, dict]:
    """获取文件或目录的信息

    .. caution::
        如果是目录，还包含其内（子目录树下）所有的文件数和目录数，数量越多，响应越久，所以对于目录要慎用

    :param client: 115 客户端或 cookies
    :param id: 文件或目录的 id 或 pickcode
    :param id_to_dirnode: 字典，保存 id 到对应文件的 ``(name, parent_id)`` 元组的字典
    :param app: 使用指定 app（设备）的接口
    :param async_: 是否异步
    :param request_kwargs: 其它请求参数

    :return: 文件或目录的信息
    """
    if isinstance(client, (str, PathLike)):
        client = P115Client(client, check_for_relogin=True)
    if id_to_dirnode is None:
        id_to_dirnode = ID_TO_DIRNODE_CACHE[client.user_id]
    try:
        id = to_id(id)
    except ValueError:
        if isinstance(client, P115Client) and app != "open":
            raise
    def gen_step():
        if not isinstance(client, P115Client) or app == "open":
            resp = yield client.fs_info_open(
                id, 
                async_=async_, 
                **request_kwargs, 
            )
        elif app in ("", "web", "desktop", "harmony", "aps"):
            resp = yield client.fs_category_get(
                id, 
                async_=async_, 
                **request_kwargs, 
            )
        else:
            resp = yield client.fs_category_get_app(
                id, 
                app=app, 
                async_=async_, 
                **request_kwargs, 
            )
        return update_resp_ancestors(resp, id_to_dirnode, error=FileNotFoundError(errno.ENOENT, f"not found: {id!r}"))
    return run_gen_step(gen_step, async_)


@overload
def iter_list(
    client: str | PathLike | P115Client | P115OpenClient, 
    cid: int | str = 0, 
    start: int = 0, 
    page_size: int = 7_000, 
    first_page_size: int = 0, 
    payload: None | dict = None, 
    normalize_attr: None | Callable[[dict], dict] = normalize_attr, 
    id_to_dirnode: None | EllipsisType | MutableMapping[int, tuple[str, int]] = None, 
    app: str = "web", 
    *, 
    async_: Literal[False] = False, 
    **request_kwargs, 
) -> Iterator[dict]:
    ...
@overload
def iter_list(
    client: str | PathLike | P115Client | P115OpenClient, 
    cid: int | str = 0, 
    start: int = 0, 
    page_size: int = 7_000, 
    first_page_size: int = 0, 
    payload: None | dict = None, 
    normalize_attr: None | Callable[[dict], dict] = normalize_attr, 
    id_to_dirnode: None | EllipsisType | MutableMapping[int, tuple[str, int]] = None, 
    app: str = "web", 
    *, 
    async_: Literal[True], 
    **request_kwargs, 
) -> AsyncIterator[dict]:
    ...
def iter_list(
    client: str | PathLike | P115Client | P115OpenClient, 
    cid: int | str = 0, 
    start: int = 0, 
    page_size: int = 7_000, 
    first_page_size: int = 0, 
    payload: None | dict = None, 
    normalize_attr: None | Callable[[dict], dict] = normalize_attr, 
    id_to_dirnode: None | EllipsisType | MutableMapping[int, tuple[str, int]] = None, 
    app: str = "web", 
    *, 
    async_: Literal[False, True] = False, 
    **request_kwargs, 
) -> Iterator[dict] | AsyncIterator[dict]:
    """在某个目录下面，迭代获取直属的文件或目录列表（逐页拉取）

    :param client: 115 客户端或 cookies
    :param cid: 目录的 id 或 pickcode
    :param start: 开始索引（从 0 开始）
    :param page_size: 分页大小，如果 <= 0，则自动确定
    :param first_page_size: 首次拉取的分页大小，如果 <= 0，则自动确定
    :param payload: 其它的查询参数
    :param normalize_attr: 把数据进行转换处理，使之便于阅读
    :param id_to_dirnode: 字典，保存 id 到对应文件的 ``(name, parent_id)`` 元组的字典
    :param app: 使用指定 app（设备）的接口
    :param async_: 是否异步
    :param request_kwargs: 其它请求参数

    :return: 迭代器，每迭代一次执行一次分页拉取请求（就像瀑布流）
    """
    if isinstance(client, (str, PathLike)):
        client = P115Client(client, check_for_relogin=True)
    if id_to_dirnode is None:
        id_to_dirnode = ID_TO_DIRNODE_CACHE[client.user_id]
    cid = to_id(cid)
    def gen_step():
        with with_iter_next(iter_fs_files_serialized(
            client, 
            dict(payload or (), cid=cid, offset=start), 
            page_size=page_size, 
            first_page_size=first_page_size, 
            app=app, 
            async_=async_, 
            **request_kwargs, 
        )) as get_next:
            while True:
                resp = yield get_next()
                update_resp_ancestors(resp, id_to_dirnode, error=FileNotFoundError(errno.ENOENT, f"not found: {cid!r}"))
                if id_to_dirnode is not ...:
                    for attr in filter(attrgetter("is_dir"), map(overview_attr, resp["data"])):
                        id_to_dirnode[attr.id] = (attr.name, attr.parent_id)
                if normalize_attr:
                    resp["data"] = list(map(normalize_attr, resp["data"]))
                yield Yield(resp)
    return run_gen_step_iter(gen_step, async_)


@overload
def get_ancestors(
    client: str | PathLike | P115Client | P115OpenClient, 
    attr: int | str | dict = 0, 
    id_to_dirnode: None | EllipsisType | MutableMapping[int, tuple[str, int]] = None, 
    ensure_file: None | bool = None, 
    refresh: bool = False, 
    app: str = "web", 
    *, 
    async_: Literal[False] = False, 
    **request_kwargs, 
) -> list[dict]:
    ...
@overload
def get_ancestors(
    client: str | PathLike | P115Client | P115OpenClient, 
    attr: int | str | dict = 0, 
    id_to_dirnode: None | EllipsisType | MutableMapping[int, tuple[str, int]] = None, 
    ensure_file: None | bool = None, 
    refresh: bool = False, 
    app: str = "web", 
    *, 
    async_: Literal[True], 
    **request_kwargs, 
) -> Coroutine[Any, Any, list[dict]]:
    ...
def get_ancestors(
    client: str | PathLike | P115Client | P115OpenClient, 
    attr: int | str | dict = 0, 
    id_to_dirnode: None | EllipsisType | MutableMapping[int, tuple[str, int]] = None, 
    ensure_file: None | bool = None, 
    refresh: bool = False, 
    app: str = "web", 
    *, 
    async_: Literal[False, True] = False, 
    **request_kwargs, 
) -> list[dict] | Coroutine[Any, Any, list[dict]]:
    """获取某个节点对应的祖先节点列表（只有 "id"、"parent_id" 和 "name" 的信息）

    :param client: 115 客户端或 cookies
    :param attr: 待查询节点 id 或 pickcode 或信息字典（必须有 "id"，可选有 "parent_id" 或 "is_dir"）
    :param id_to_dirnode: 字典，保存 id 到对应文件的 ``(name, parent_id)`` 元组的字典
    :param ensure_file: 是否确保为文件

        - True:  确定是文件
        - False: 确定是目录
        - None:  不确定

    :param refresh: 是否强制刷新，如果为 False，则尽量从 ``id_to_dirnode`` 获取，以减少接口调用
    :param app: 使用指定 app（设备）的接口
    :param async_: 是否异步
    :param request_kwargs: 其它请求参数

    :return: 目录所对应的祖先信息列表，每一条的结构如下

        .. code:: python

            {
                "id": int, # 目录的 id
                "parent_id": int, # 上级目录的 id
                "name": str, # 名字
            }
    """
    if isinstance(client, (str, PathLike)):
        client = P115Client(client, check_for_relogin=True)
    if id_to_dirnode is None:
        id_to_dirnode = ID_TO_DIRNODE_CACHE[client.user_id]
    def get_resp_by_info(id: int, /):
        return get_info(
            client, 
            id, 
            id_to_dirnode=id_to_dirnode, 
            app=app, 
            async_=async_, 
            **request_kwargs, 
        )
    do_next: Callable = anext if async_ else next
    def get_resp_by_list(cid: int, /):
        return do_next(iter_list(
            client, 
            cid, 
            page_size=1, 
            payload={"cur": 1, "nf": 1, "star": 1, "hide_data": 1}, 
            id_to_dirnode=id_to_dirnode, 
            normalize_attr=None, 
            app=app, 
            async_=async_, 
            **request_kwargs, 
        ))
    def get_resp(id: int, /, ensure_file: None | bool = None):
        if ensure_file is None:
            try:
                return get_resp_by_list(id)
            except (FileNotFoundError, NotADirectoryError):
                return get_resp_by_info(id)
        elif ensure_file:
            return get_resp_by_info(id)
        else:
            return get_resp_by_list(id)
    def gen_step():
        nonlocal attr, ensure_file
        ancestors: list[dict] = [{"id": 0, "parent_id": 0, "name": ""}]
        if not attr:
            return ancestors
        if isinstance(attr, dict):
            if not (fid := int(attr["id"])):
                return ancestors
            is_dir: None | bool = attr.get("is_dir")
            if is_dir is None:
                if "parent_id" in attr:
                    pid = int(attr["parent_id"])
                    ancestors = yield get_ancestors(
                        client, 
                        pid, 
                        id_to_dirnode=id_to_dirnode, 
                        ensure_file=False, 
                        refresh=refresh, 
                        app=app, 
                        async_=async_, 
                        **request_kwargs, 
                    )
                    name = ""
                    if "name" in attr:
                        name = attr["name"]
                    elif isinstance(client, P115Client):
                        attr = yield get_attr(
                            client, 
                            fid, 
                            skim=True, 
                            async_=async_, 
                            **request_kwargs, 
                        )
                        name = cast(dict, attr)["name"]
                    if name:
                        ancestors.append({"id": fid, "parent_id": pid, "name": name})
                        return ancestors
            else:
                ensure_file = not is_dir
        elif not (fid := to_id(attr)):
            return ancestors
        if not refresh and id_to_dirnode is not ... and fid in id_to_dirnode:
            parts: list[dict] = []
            add_part = parts.append
            try:
                cid = fid
                while cid:
                    id = cid
                    name, cid = id_to_dirnode[cid]
                    add_part({"id": id, "name": name, "parent_id": cid})
                ancestors.extend(reversed(parts))
                return ancestors
            except KeyError:
                pass
        resp = yield get_resp(fid, ensure_file)
        return resp["ancestors"]
    return run_gen_step(gen_step, async_)


@overload
def get_path(
    client: str | PathLike | P115Client | P115OpenClient, 
    attr: int | str | dict = 0, 
    root_id: None | int | str = None, 
    escape: None | bool | Callable[[str], str] = True, 
    id_to_dirnode: None | EllipsisType | MutableMapping[int, tuple[str, int]] = None, 
    ensure_file: None | bool = None, 
    refresh: bool = False, 
    app: str = "web", 
    *, 
    async_: Literal[False] = False, 
    **request_kwargs, 
) -> str:
    ...
@overload
def get_path(
    client: str | PathLike | P115Client | P115OpenClient, 
    attr: int | str | dict = 0, 
    root_id: None | int | str = None, 
    escape: None | bool | Callable[[str], str] = True, 
    id_to_dirnode: None | EllipsisType | MutableMapping[int, tuple[str, int]] = None, 
    ensure_file: None | bool = None, 
    refresh: bool = False, 
    app: str = "web", 
    *, 
    async_: Literal[True], 
    **request_kwargs, 
) -> Coroutine[Any, Any, str]:
    ...
def get_path(
    client: str | PathLike | P115Client | P115OpenClient, 
    attr: int | str | dict = 0, 
    root_id: None | int | str = None, 
    escape: None | bool | Callable[[str], str] = True, 
    id_to_dirnode: None | EllipsisType | MutableMapping[int, tuple[str, int]] = None, 
    ensure_file: None | bool = None, 
    refresh: bool = False, 
    app: str = "web", 
    *, 
    async_: Literal[False, True] = False, 
    **request_kwargs, 
) -> str | Coroutine[Any, Any, str]:
    """获取目录对应的路径（绝对路径或相对路径）

    :param client: 115 客户端或 cookies
    :param attr: 待查询节点 id 或 pickcode 或信息字典（必须有 "id"，可选有 "parent_id" 或 "is_dir"）
    :param root_id: 根目录 id 或 pickcode，如果指定此参数且不为 None，则返回相对路径，否则返回绝对路径
    :param escape: 对文件名进行转义

        - 如果为 None，则不处理；否则，这个函数用来对文件名中某些符号进行转义，例如 "/" 等
        - 如果为 True，则使用 `posixpatht.escape`，会对文件名中 "/"，或单独出现的 "." 和 ".." 用 "\\" 进行转义
        - 如果为 False，则使用 `posix_escape_name` 函数对名字进行转义，会把文件名中的 "/" 转换为 "|"
        - 如果为 Callable，则用你所提供的调用，以或者转义后的名字

    :param id_to_dirnode: 字典，保存 id 到对应文件的 ``(name, parent_id)`` 元组的字典
    :param ensure_file: 是否确保为文件

        - True:  确定是文件
        - False: 确定是目录
        - None:  不确定

    :param refresh: 是否刷新。如果为 True，则会执行网络请求以查询；如果为 False，则直接从 `id_to_dirnode` 中获取
    :param app: 使用指定 app（设备）的接口
    :param async_: 是否异步
    :param request_kwargs: 其它请求参数

    :return: 目录对应的绝对路径或相对路径
    """
    if isinstance(escape, bool):
        if escape:
            from posixpatht import escape
        else:
            escape = posix_escape_name
    escape = cast(None | Callable[[str], str], escape)
    if root_id is not None:
        root_id = to_id(root_id)
    def gen_step():
        if root_id is not None and (
            not attr or 
            (int(attr["id"]) if isinstance(attr, dict) else to_id(attr)) == root_id
        ):
            return ""
        ancestors = yield get_ancestors(
            client, 
            attr, 
            id_to_dirnode=id_to_dirnode, 
            ensure_file=ensure_file, 
            refresh=refresh, 
            app=app, 
            async_=async_, # type: ignore
            **request_kwargs, 
        )
        if root_id is None:
            parts = (a["name"] for a in ancestors)
        elif ancestors[0]["id"] == root_id:
            parts = (a["name"] for a in ancestors[1:])
        else:
            parts = (a["name"] for a in dropwhile(lambda a: a["parent_id"] != root_id, ancestors))
        if escape is None:
            return "/".join(parts)
        else:
            return "/".join(map(escape, parts))
    return run_gen_step(gen_step, async_)


@overload
def get_id(
    client: str | PathLike | P115Client | P115OpenClient, 
    id: int = -1, 
    pickcode: str = "", 
    sha1: str = "", 
    name: str = "", 
    path: str | Sequence[str] = "", 
    value: int | str | Sequence[str] = "", 
    size: int = -1, 
    cid: int = 0, 
    ensure_file: None | bool = None, 
    is_posixpath: bool = False, 
    refresh: bool = False, 
    id_to_dirnode: None | EllipsisType | MutableMapping[int, tuple[str, int]] = None, 
    app: str = "web", 
    dont_use_getid: bool = False, 
    *, 
    async_: Literal[False] = False, 
    **request_kwargs, 
) -> int:
    ...
@overload
def get_id(
    client: str | PathLike | P115Client | P115OpenClient, 
    id: int = -1, 
    pickcode: str = "", 
    sha1: str = "", 
    name: str = "", 
    path: str | Sequence[str] = "", 
    value: int | str | Sequence[str] = "", 
    size: int = -1, 
    cid: int = 0, 
    ensure_file: None | bool = None, 
    is_posixpath: bool = False, 
    refresh: bool = False, 
    id_to_dirnode: None | EllipsisType | MutableMapping[int, tuple[str, int]] = None, 
    app: str = "web", 
    dont_use_getid: bool = False, 
    *, 
    async_: Literal[True], 
    **request_kwargs, 
) -> Coroutine[Any, Any, int]:
    ...
def get_id(
    client: str | PathLike | P115Client | P115OpenClient, 
    id: int = -1, 
    pickcode: str = "", 
    sha1: str = "", 
    name: str = "", 
    path: str | Sequence[str] = "", 
    value: int | str | Sequence[str] = "", 
    size: int = -1, 
    cid: int = 0, 
    ensure_file: None | bool = None, 
    is_posixpath: bool = False, 
    refresh: bool = False, 
    id_to_dirnode: None | EllipsisType | MutableMapping[int, tuple[str, int]] = None, 
    app: str = "web", 
    dont_use_getid: bool = False, 
    *, 
    async_: Literal[False, True] = False, 
    **request_kwargs, 
) -> int | Coroutine[Any, Any, int]:
    """获取 id

    .. note::
        优先级，``id > pickcode > name > path > value``

    :param client: 115 客户端或 cookies
    :param id: id
    :param pickcode: 提取码
    :param sha1: 文件的 sha1 散列值
    :param name: 名称
    :param path: 路径
    :param value: 当 ``id``、``pickcode``、``name`` 和 ``path`` 不可用时生效，将会自动决定所属类型
    :param size: 文件大小
    :param cid: 顶层目录 id
    :param ensure_file: 是否确保为文件

        - True: 必须是文件
        - False: 必须是目录
        - None: 可以是目录或文件

    :param is_posixpath: 使用 posixpath，会把 "/" 转换为 "|"，因此解析的时候，会对 "|" 进行特别处理
    :param refresh: 是否刷新。如果为 True，则会执行网络请求以查询；如果为 False，则直接从 `id_to_dirnode` 中获取
    :param id_to_dirnode: 字典，保存 id 到对应文件的 ``(name, parent_id)`` 元组的字典
    :param app: 使用指定 app（设备）的接口
    :param dont_use_getid: 不要使用 `client.fs_dir_getid` 或 `client.fs_dir_getid_app`，以便 `id_to_dirnode` 有缓存
    :param async_: 是否异步
    :param request_kwargs: 其它请求参数

    :return: 文件或目录的 id
    """
    if id >= 0:
        if id or not ensure_file:
            return id
    if pickcode:
        return to_id(pickcode)
    elif sha1:
        return get_id_to_sha1(
            client, 
            sha1=sha1, 
            size=size, 
            cid=cid, 
            app=app, 
            async_=async_, 
            **request_kwargs, 
        )
    elif name:
        return get_id_to_name(
            client, 
            name=name, 
            size=size, 
            cid=cid, 
            ensure_file=ensure_file, 
            app=app, 
            async_=async_, 
            **request_kwargs, 
        )
    elif path:
        return get_id_to_path(
            client, 
            path=path, 
            cid=cid, 
            ensure_file=ensure_file, 
            is_posixpath=is_posixpath, 
            refresh=refresh, 
            id_to_dirnode=id_to_dirnode, 
            app=app, 
            dont_use_getid=dont_use_getid, 
            async_=async_, 
            **request_kwargs, 
        )
    else:
        if isinstance(value, (int, str)):
            if is_valid_id(value):
                id = int(value)
                if id or not ensure_file:
                    return id
                value = str(id)
            value = cast(str, value)
            if is_valid_pickcode(value):
                return to_id(value)
            elif is_valid_sha1(value):
                return get_id_to_sha1(
                    client, 
                    sha1=value, 
                    size=size, 
                    cid=cid, 
                    app=app, 
                    async_=async_, 
                    **request_kwargs, 
                )
            elif is_valid_name(value):
                return get_id_to_name(
                    client, 
                    name=value, 
                    size=size, 
                    cid=cid, 
                    ensure_file=ensure_file, 
                    app=app, 
                    async_=async_, 
                    **request_kwargs, 
                )
        return get_id_to_path(
            client, 
            path=value, 
            cid=cid, 
            ensure_file=ensure_file, 
            is_posixpath=is_posixpath, 
            refresh=refresh, 
            id_to_dirnode=id_to_dirnode, 
            app=app, 
            dont_use_getid=dont_use_getid, 
            async_=async_, 
            **request_kwargs, 
        )


@overload
def get_id_to_path(
    client: str | PathLike | P115Client | P115OpenClient, 
    path: str | Sequence[str], 
    cid: int = 0, 
    ensure_file: None | bool = None, 
    is_posixpath: bool = False, 
    refresh: bool = False, 
    id_to_dirnode: None | EllipsisType | MutableMapping[int, tuple[str, int]] = None, 
    app: str = "web", 
    dont_use_getid: bool = False, 
    *, 
    async_: Literal[False] = False, 
    **request_kwargs, 
) -> int:
    ...
@overload
def get_id_to_path(
    client: str | PathLike | P115Client | P115OpenClient, 
    path: str | Sequence[str], 
    cid: int = 0, 
    ensure_file: None | bool = None, 
    is_posixpath: bool = False, 
    refresh: bool = False, 
    id_to_dirnode: None | EllipsisType | MutableMapping[int, tuple[str, int]] = None, 
    app: str = "web", 
    dont_use_getid: bool = False, 
    *, 
    async_: Literal[True], 
    **request_kwargs, 
) -> Coroutine[Any, Any, int]:
    ...
def get_id_to_path(
    client: str | PathLike | P115Client | P115OpenClient, 
    path: str | Sequence[str], 
    cid: int = 0, 
    ensure_file: None | bool = None, 
    is_posixpath: bool = False, 
    refresh: bool = False, 
    id_to_dirnode: None | EllipsisType | MutableMapping[int, tuple[str, int]] = None, 
    app: str = "web", 
    dont_use_getid: bool = False, 
    *, 
    async_: Literal[False, True] = False, 
    **request_kwargs, 
) -> int | Coroutine[Any, Any, int]:
    """获取 path 对应的 id

    :param client: 115 客户端或 cookies
    :param path: 路径
    :param cid: 顶层目录的 id
    :param ensure_file: 是否确保为文件

        - True: 必须是文件
        - False: 必须是目录
        - None: 可以是目录或文件

    :param is_posixpath: 使用 posixpath，会把 "/" 转换为 "|"，因此解析的时候，会对 "|" 进行特别处理
    :param refresh: 是否刷新。如果为 True，则会执行网络请求以查询；如果为 False，则直接从 `id_to_dirnode` 中获取
    :param id_to_dirnode: 字典，保存 id 到对应文件的 ``(name, parent_id)`` 元组的字典
    :param app: 使用指定 app（设备）的接口
    :param dont_use_getid: 不要使用 `client.fs_dir_getid` 或 `client.fs_dir_getid_app`，以便 `id_to_dirnode` 有缓存
    :param async_: 是否异步
    :param request_kwargs: 其它请求参数

    :return: 文件或目录的 id
    """
    if isinstance(client, (str, PathLike)):
        client = P115Client(client, check_for_relogin=True)
    if id_to_dirnode is None:
        id_to_dirnode = ID_TO_DIRNODE_CACHE[client.user_id]
    error = FileNotFoundError(errno.ENOENT, f"no such path: {path!r}")
    def gen_step():
        nonlocal ensure_file, cid, path
        if isinstance(path, str):
            if path.startswith("/"):
                cid = 0
            if path in (".", "..", "/"):
                if ensure_file:
                    raise error
                return cid
            elif path.startswith("根目录 > "):
                cid = 0
                patht = path.split(" > ")[1:]
            elif is_posixpath:
                if ensure_file is None and path.endswith("/"):
                    ensure_file = False
                patht = [p for p in path.split("/") if p]
            else:
                if ensure_file is None and path_is_dir_form(path):
                    ensure_file = False
                patht, _ = splits(path.lstrip("/"))
        else:
            if path and not path[0]:
                cid = 0
                patht = list(path[1:])
            else:
                patht = [p for p in path if p]
            if not patht:
                return cid
        if not patht:
            if ensure_file:
                raise error
            return cid
        if not isinstance(client, P115Client) or app == "open":
            resp = yield get_info(
                client, 
                ">" + ">".join(patht), 
                id_to_dirnode=id_to_dirnode, 
                app="open", 
                async_=async_, 
                **request_kwargs, 
            )
            return P115ID(resp["id"], resp, about="path")
        i = 0
        start_parent_id = cid
        if not refresh and id_to_dirnode and id_to_dirnode is not ...:
            if i := len(patht) - bool(ensure_file):
                obj = "|" if is_posixpath else "/"
                for i in range(i):
                    if obj in patht[i]:
                        break
                else:
                    i += 1
            if i:
                for i in range(i):
                    needle = (patht[i], cid)
                    for fid, key in id_to_dirnode.items():
                        if needle == key:
                            cid = fid
                            break
                    else:
                        break
                else:
                    i += 1
        if i == len(patht):
            return cid
        if not start_parent_id:
            stop = 0
            if j := len(patht) - bool(ensure_file):
                for stop, part in enumerate(patht[:j]):
                    if "/" in part:
                        break
                else:
                    stop += 1
            if not dont_use_getid:
                while stop > i:
                    if app in ("", "web", "desktop", "harmony", "aps"):
                        fs_dir_getid: Callable = client.fs_dir_getid
                    else:
                        fs_dir_getid = partial(client.fs_dir_getid_app, app=app)
                    dirname = "/".join(patht[:stop])
                    resp = yield fs_dir_getid(dirname, async_=async_, **request_kwargs)
                    check_response(resp)
                    pid = int(resp["id"])
                    if not pid:
                        if stop == len(patht) and ensure_file is None:
                            stop -= 1
                            continue
                        raise error
                    cid = pid
                    i = stop
                    break
        if i == len(patht):
            return cid
        for name in patht[i:-1]:
            if is_posixpath:
                name = name.replace("/", "|")
            with with_iter_next(iterdir(
                client, 
                cid, 
                ensure_file=False, 
                app=app, 
                id_to_dirnode=id_to_dirnode, 
                async_=async_, 
                **request_kwargs, 
            )) as get_next:
                found = False
                while not found:
                    attr = yield get_next()
                    found = (attr["name"].replace("/", "|") if is_posixpath else attr["name"]) == name
                    cid = attr["id"]
                if not found:
                    raise error
        name = patht[-1]
        if is_posixpath:
            name = name.replace("/", "|")
        with with_iter_next(iterdir(
            client, 
            cid, 
            app=app, 
            id_to_dirnode=id_to_dirnode, 
            async_=async_, 
            **request_kwargs, 
        )) as get_next:
            while True:
                attr = yield get_next()
                if (attr["name"].replace("/", "|") if is_posixpath else attr["name"]) == name:
                    if ensure_file is None or ensure_file ^ attr["is_dir"]:
                        return P115ID(attr["id"], attr, about="path",)
        raise error
    return run_gen_step(gen_step, async_)


@overload
def get_id_to_sha1(
    client: str | PathLike | P115Client | P115OpenClient, 
    sha1: str, 
    size: int = -1, 
    cid: int | str = 0, 
    app: str = "web", 
    *, 
    async_: Literal[False] = False, 
    **request_kwargs, 
) -> P115ID:
    ...
@overload
def get_id_to_sha1(
    client: str | PathLike | P115Client | P115OpenClient, 
    sha1: str, 
    size: int = -1, 
    cid: int | str = 0, 
    app: str = "web", 
    *, 
    async_: Literal[True], 
    **request_kwargs, 
) -> Coroutine[Any, Any, P115ID]:
    ...
def get_id_to_sha1(
    client: str | PathLike | P115Client | P115OpenClient, 
    sha1: str, 
    size: int = -1, 
    cid: int | str = 0, 
    app: str = "web", 
    *, 
    async_: Literal[False, True] = False, 
    **request_kwargs, 
) -> P115ID | Coroutine[Any, Any, P115ID]:
    """获取 sha1 对应的文件的 id

    .. caution::
        这个函数并不会检查输入的 ``sha1`` 是否合法

    :param client: 115 客户端或 cookies
    :param sha1: 文件的 sha1 哈希值
    :param size: 文件大小
    :param cid: 顶层目录 id
    :param app: 使用指定 app（设备）的接口
    :param async_: 是否异步
    :param request_kwargs: 其它请求参数

    :return: 文件或目录的 id
    """
    sha1 = sha1.upper()
    assert size or sha1 == "DA39A3EE5E6B4B0D3255BFEF95601890AFD80709"
    if isinstance(client, (str, PathLike)):
        client = P115Client(client, check_for_relogin=True)
    def gen_step():
        search: None | Callable = None
        if not isinstance(client, P115Client) or app == "open":
            search = client.fs_search_open
        elif app in ("", "web", "desktop", "harmony", "aps"):
            if not cid and size < 0:
                resp: dict = yield client.fs_shasearch(sha1, async_=async_, **request_kwargs)
                check_response(resp)
                attr = normalize_attr(resp["data"])
                return P115ID(attr["id"], attr, about="sha1", sha1=sha1)
            else:
                search = client.fs_search
        else:
            search = partial(client.fs_search_app, app=app)
        if search is not None:
            payload = {"cid": cid, "fc": 0, "limit": 100, "search_value": sha1}
            for offset in range(0, 10_000, 100):
                if offset and resp["count"] <= offset:
                    break
                payload["offset"] = offset
                resp = yield search(payload, async_=async_, **request_kwargs)
                check_response(resp)
                for attr in map(normalize_attr, resp["data"]):
                    if attr["sha1"] != sha1:
                        break
                    if size < 0 or attr["size"] == size:
                        return P115ID(attr["id"], attr, about="sha1", sha1=sha1)
        raise P115FileNotFoundError(
            errno.ENOENT, 
            {"state": False, "user_id": client.user_id, "sha1": sha1, "size": size, "cid": cid, "error": "not found"}, 
        )
    return run_gen_step(gen_step, async_)


@overload
def get_id_to_name(
    client: str | PathLike | P115Client | P115OpenClient, 
    name: str, 
    size: int = -1, 
    cid: int | str = 0, 
    ensure_file: None | bool = None, 
    app: str = "web", 
    *, 
    async_: Literal[False] = False, 
    **request_kwargs, 
) -> P115ID:
    ...
@overload
def get_id_to_name(
    client: str | PathLike | P115Client | P115OpenClient, 
    name: str, 
    size: int = -1, 
    cid: int | str = 0, 
    ensure_file: None | bool = None, 
    app: str = "web", 
    *, 
    async_: Literal[True], 
    **request_kwargs, 
) -> Coroutine[Any, Any, P115ID]:
    ...
def get_id_to_name(
    client: str | PathLike | P115Client | P115OpenClient, 
    name: str, 
    size: int = -1, 
    cid: int | str = 0, 
    ensure_file: None | bool = None, 
    app: str = "web", 
    *, 
    async_: Literal[False, True] = False, 
    **request_kwargs, 
) -> P115ID | Coroutine[Any, Any, P115ID]:
    """获取 name 对应的文件的 id

    .. caution::
        这个函数并不会检查输入的 ``name`` 是否合法

    :param client: 115 客户端或 cookies
    :param name: 文件名
    :param size: 文件大小
    :param cid: 顶层目录 id
    :param ensure_file: 是否确保为文件

        - True:  确定是文件
        - False: 确定是目录
        - None:  不确定

    :param app: 使用指定 app（设备）的接口
    :param async_: 是否异步
    :param request_kwargs: 其它请求参数

    :return: 文件或目录的 id
    """
    assert name
    if isinstance(client, (str, PathLike)):
        client = P115Client(client, check_for_relogin=True)
    def gen_step():
        if not isinstance(client, P115Client) or app == "open":
            search: Callable = client.fs_search_open
        elif app in ("", "web", "desktop", "harmony", "aps"):
            search = client.fs_search
        else:
            search = partial(client.fs_search_app, app=app)
        payload = {"cid": cid, "limit": 10_000, "search_value": name}
        if ensure_file:
            payload["fc"] = 0
            suffix = name.rpartition(".")[-1]
            if suffix.isalnum():
                payload["suffix"] = suffix
        elif ensure_file is False:
            payload["fc"] = 1
        resp: dict = yield search(payload, async_=async_, **request_kwargs)
        if ensure_file and get_first(resp, "errno", "errNo", default=0) == 20021:
            payload.pop("suffix")
            resp = yield search(payload, async_=async_, **request_kwargs)
        check_response(resp)
        for attr in map(normalize_attr, resp["data"]):
            if attr["name"] == name and (
                not ensure_file or 
                size < 0 or 
                attr["size"] == size
            ):
                return P115ID(attr["id"], attr, about="name", name=name)
        raise P115FileNotFoundError(
            errno.ENOENT, 
            {"state": False, "user_id": client.user_id, "name": name, "size": size, "cid": cid, "error": "not found"}, 
        )
    return run_gen_step(gen_step, async_)


@overload
def share_get_id(
    client: str | PathLike | P115Client, 
    share_code: str, 
    receive_code: str = "", 
    id: int = -1, 
    name: str = "", 
    path: str | Sequence[str] = "", 
    value: int | str | Sequence[str] = "", 
    size: int = -1, 
    cid: int = 0, 
    ensure_file: None | bool = None, 
    is_posixpath: bool = False,
    refresh: bool = False, 
    id_to_dirnode: None | EllipsisType | MutableMapping[int, tuple[str, int]] = None, 
    *, 
    async_: Literal[False] = False, 
    **request_kwargs, 
) -> int:
    ...
@overload
def share_get_id(
    client: str | PathLike | P115Client, 
    share_code: str, 
    receive_code: str = "", 
    id: int = -1, 
    name: str = "", 
    path: str | Sequence[str] = "", 
    value: int | str | Sequence[str] = "", 
    size: int = -1, 
    cid: int = 0, 
    ensure_file: None | bool = None, 
    is_posixpath: bool = False,
    refresh: bool = False, 
    id_to_dirnode: None | EllipsisType | MutableMapping[int, tuple[str, int]] = None, 
    *, 
    async_: Literal[True], 
    **request_kwargs, 
) -> Coroutine[Any, Any, int]:
    ...
def share_get_id(
    client: str | PathLike | P115Client, 
    share_code: str, 
    receive_code: str = "", 
    id: int = -1, 
    name: str = "", 
    path: str | Sequence[str] = "", 
    value: int | str | Sequence[str] = "", 
    size: int = -1, 
    cid: int = 0, 
    ensure_file: None | bool = None, 
    is_posixpath: bool = False,
    refresh: bool = False, 
    id_to_dirnode: None | EllipsisType | MutableMapping[int, tuple[str, int]] = None, 
    *, 
    async_: Literal[False, True] = False, 
    **request_kwargs, 
) -> int | Coroutine[Any, Any, int]:
    """对分享链接，获取 id

    .. note::
        优先级，``name > path > value``

    :param client: 115 客户端或 cookies
    :param share_code: 分享码或链接
    :param receive_code: 接收码
    :param id: id
    :param name: 名称
    :param path: 路径
    :param value: 当 ``id``、``name`` 和 ``path`` 不可用时生效，将会自动决定所属类型
    :param size: 文件大小
    :param cid: 顶层目录 id
    :param ensure_file: 是否确保为文件

        - True: 必须是文件
        - False: 必须是目录
        - None: 可以是目录或文件

    :param is_posixpath: 使用 posixpath，会把 "/" 转换为 "|"，因此解析的时候，会对 "|" 进行特别处理
    :param refresh: 是否刷新。如果为 True，则会执行网络请求以查询；如果为 False，则直接从 `id_to_dirnode` 中获取
    :param id_to_dirnode: 字典，保存 id 到对应文件的 ``(name, parent_id)`` 元组的字典
    :param async_: 是否异步
    :param request_kwargs: 其它请求参数

    :return: 文件或目录的 id
    """
    if id >= 0:
        if id or not ensure_file:
            return id
    if name:
        return share_get_id_to_name(
            client, 
            name=name, 
            share_code=share_code, 
            receive_code=receive_code, 
            size=size, 
            cid=cid, 
            ensure_file=ensure_file, 
            async_=async_, 
            **request_kwargs, 
        )
    elif path:
        return share_get_id_to_path(
            client, 
            path=path, 
            share_code=share_code, 
            receive_code=receive_code, 
            cid=cid, 
            ensure_file=ensure_file, 
            is_posixpath=is_posixpath, 
            refresh=refresh, 
            id_to_dirnode=id_to_dirnode, 
            async_=async_, 
            **request_kwargs, 
        )
    else:
        if isinstance(value, (int, str)):
            if is_valid_id(value):
                id = int(value)
                if id or not ensure_file:
                    return id
                value = str(id)
            value = cast(str, value)
            if is_valid_name(value):
                return share_get_id_to_name(
                    client, 
                    name=value, 
                    share_code=share_code, 
                    receive_code=receive_code, 
                    size=size, 
                    cid=cid, 
                    ensure_file=ensure_file, 
                    async_=async_, 
                    **request_kwargs, 
                )
        return share_get_id_to_path(
            client, 
            path=value, 
            share_code=share_code, 
            receive_code=receive_code, 
            cid=cid, 
            ensure_file=ensure_file, 
            is_posixpath=is_posixpath, 
            refresh=refresh, 
            id_to_dirnode=id_to_dirnode, 
            async_=async_, 
            **request_kwargs, 
        )


@overload
def share_get_id_to_path(
    client: str | PathLike | P115Client, 
    share_code: str, 
    receive_code: str = "", 
    path: str | Sequence[str] = "", 
    cid: int = 0, 
    ensure_file: None | bool = None, 
    is_posixpath: bool = False, 
    refresh: bool = False, 
    id_to_dirnode: None | EllipsisType | MutableMapping[int, tuple[str, int]] = None, 
    *, 
    async_: Literal[False] = False, 
    **request_kwargs, 
) -> int:
    ...
@overload
def share_get_id_to_path(
    client: str | PathLike | P115Client, 
    share_code: str, 
    receive_code: str = "", 
    path: str | Sequence[str] = "", 
    cid: int = 0, 
    ensure_file: None | bool = None, 
    is_posixpath: bool = False, 
    refresh: bool = False, 
    id_to_dirnode: None | EllipsisType | MutableMapping[int, tuple[str, int]] = None, 
    *, 
    async_: Literal[True], 
    **request_kwargs, 
) -> Coroutine[Any, Any, int]:
    ...
def share_get_id_to_path(
    client: str | PathLike | P115Client, 
    share_code: str, 
    receive_code: str = "", 
    path: str | Sequence[str] = "", 
    cid: int = 0, 
    ensure_file: None | bool = None, 
    is_posixpath: bool = False, 
    refresh: bool = False, 
    id_to_dirnode: None | EllipsisType | MutableMapping[int, tuple[str, int]] = None, 
    *, 
    async_: Literal[False, True] = False, 
    **request_kwargs, 
) -> int | Coroutine[Any, Any, int]:
    """对分享链接，获取 path 对应的 id

    :param client: 115 客户端或 cookies
    :param share_code: 分享码或链接
    :param receive_code: 接收码
    :param path: 路径
    :param cid: 顶层目录的 id
    :param ensure_file: 是否确保为文件

        - True: 必须是文件
        - False: 必须是目录
        - None: 可以是目录或文件

    :param is_posixpath: 使用 posixpath，会把 "/" 转换为 "|"，因此解析的时候，会对 "|" 进行特别处理
    :param refresh: 是否刷新。如果为 True，则会执行网络请求以查询；如果为 False，则直接从 `id_to_dirnode` 中获取
    :param id_to_dirnode: 字典，保存 id 到对应文件的 ``(name, parent_id)`` 元组的字典
    :param async_: 是否异步
    :param request_kwargs: 其它请求参数

    :return: 文件或目录的 id
    """
    if isinstance(client, (str, PathLike)):
        client = P115Client(client, check_for_relogin=True)
    def gen_step():
        nonlocal ensure_file, cid, id_to_dirnode
        payload = cast(dict, share_extract_payload(share_code))
        if receive_code:
            payload["receive_code"] = receive_code
        if id_to_dirnode is None:
            id_to_dirnode = ID_TO_DIRNODE_CACHE[payload["share_code"]]
        request_kwargs.update(payload)
        error = FileNotFoundError(errno.ENOENT, f"no such path: {path!r}")
        if isinstance(path, str):
            if path.startswith("/"):
                cid = 0
            if path in (".", "..", "/"):
                if ensure_file:
                    raise error
                return cid
            elif path.startswith("根目录 > "):
                cid = 0
                patht = path.split(" > ")[1:]
            elif is_posixpath:
                if ensure_file is None and path.endswith("/"):
                    ensure_file = False
                patht = [p for p in path.split("/") if p]
            else:
                if ensure_file is None and path_is_dir_form(path):
                    ensure_file = False
                patht, _ = splits(path.lstrip("/"))
        else:
            if path and not path[0]:
                cid = 0
                patht = list(path[1:])
            else:
                patht = [p for p in path if p]
            if not patht:
                return cid
        if not patht:
            if ensure_file:
                raise error
            return cid
        i = 0
        if not refresh and id_to_dirnode and id_to_dirnode is not ...:
            if i := len(patht) - bool(ensure_file):
                obj = "|" if is_posixpath else "/"
                for i in range(i):
                    if obj in patht[i]:
                        break
                else:
                    i += 1
            if i:
                for i in range(i):
                    needle = (patht[i], cid)
                    for fid, key in id_to_dirnode.items():
                        if needle == key:
                            cid = fid
                            break
                    else:
                        break
                else:
                    i += 1
        if i == len(patht):
            return cid
        for name in patht[i:-1]:
            if is_posixpath:
                name = name.replace("/", "|")
            with with_iter_next(share_iterdir(
                client, 
                cid=cid, 
                id_to_dirnode=id_to_dirnode, 
                async_=async_, 
                **request_kwargs, 
            )) as get_next:
                found = False
                while not found:
                    attr = yield get_next()
                    found = attr["is_dir"] and (attr["name"].replace("/", "|") if is_posixpath else attr["name"]) == name
                    cid = attr["id"]
            if not found:
                raise error
        name = patht[-1]
        if is_posixpath:
            name = name.replace("/", "|")
        with with_iter_next(share_iterdir(
            client, 
            cid=cid, 
            id_to_dirnode=id_to_dirnode, 
            async_=async_, 
            **request_kwargs, 
        )) as get_next:
            while True:
                attr = yield get_next()
                if (attr["name"].replace("/", "|") if is_posixpath else attr["name"]) == name:
                    if ensure_file is None or ensure_file ^ attr["is_dir"]:
                        return P115ID(attr["id"], attr, about="path")
        raise error
    return run_gen_step(gen_step, async_)


@overload
def share_get_id_to_name(
    client: str | PathLike | P115Client, 
    name: str, 
    share_code: str, 
    receive_code: str = "", 
    size: int = -1, 
    cid: int | str = 0, 
    ensure_file: None | bool = None, 
    *, 
    async_: Literal[False] = False, 
    **request_kwargs, 
) -> P115ID:
    ...
@overload
def share_get_id_to_name(
    client: str | PathLike | P115Client, 
    name: str, 
    share_code: str, 
    receive_code: str = "", 
    size: int = -1, 
    cid: int | str = 0, 
    ensure_file: None | bool = None, 
    *, 
    async_: Literal[True], 
    **request_kwargs, 
) -> Coroutine[Any, Any, P115ID]:
    ...
def share_get_id_to_name(
    client: str | PathLike | P115Client, 
    name: str, 
    share_code: str, 
    receive_code: str = "", 
    size: int = -1, 
    cid: int | str = 0, 
    ensure_file: None | bool = None, 
    *, 
    async_: Literal[False, True] = False, 
    **request_kwargs, 
) -> P115ID | Coroutine[Any, Any, P115ID]:
    """对分享链接，获取 sha1 对应的文件的 id

    .. caution::
        这个函数并不会检查输入的 ``name`` 是否合法

    :param client: 115 客户端或 cookies
    :param name: 文件名
    :param share_code: 分享码或链接
    :param receive_code: 接收码
    :param size: 文件大小
    :param cid: 顶层目录 id
    :param ensure_file: 是否确保为文件

        - True:  确定是文件
        - False: 确定是目录
        - None:  不确定

    :param app: 使用指定 app（设备）的接口
    :param async_: 是否异步
    :param request_kwargs: 其它请求参数

    :return: 文件或目录的 id
    """
    assert share_code and name
    if isinstance(client, (str, PathLike)):
        client = P115Client(client, check_for_relogin=True)
    def gen_step():
        search = client.share_search
        payload = cast(dict, share_extract_payload(share_code))
        if receive_code:
            payload["receive_code"] = receive_code
        payload.update({
            "cid": cid, 
            "limit": 10_000, 
            "search_value": name, 
        })
        if ensure_file:
            payload["fc"] = 0
            suffix = name.rpartition(".")[-1]
            if suffix.isalnum():
                payload["suffix"] = suffix
        elif ensure_file is False:
            payload["fc"] = 1
        resp: dict = yield search(payload, async_=async_, **request_kwargs)
        if ensure_file and get_first(resp, "errno", "errNo", default=0) == 20021:
            payload.pop("suffix")
            resp = yield search(payload, async_=async_, **request_kwargs)
        check_response(resp)
        for attr in map(normalize_attr, resp["data"]["list"]):
            if attr["name"] == name and (
                not ensure_file or 
                size < 0 or 
                attr["size"] == size
            ):
                return P115ID(attr["id"], attr, about="name", name=name)
        raise P115FileNotFoundError(
            errno.ENOENT, 
            {"state": False, "share_code": share_code, "name": name, "size": size, "cid": cid, "error": "not found"}, 
        )
    return run_gen_step(gen_step, async_)


@overload
def get_file_count(
    client: str | PathLike | P115Client, 
    cid: int | str = 0, 
    id_to_dirnode: None | EllipsisType | MutableMapping[int, tuple[str, int]] = None, 
    app: str = "web", 
    use_fs_files: bool = False, 
    *, 
    async_: Literal[False] = False, 
    **request_kwargs, 
) -> int:
    ...
@overload
def get_file_count(
    client: str | PathLike | P115Client, 
    cid: int | str = 0, 
    id_to_dirnode: None | EllipsisType | MutableMapping[int, tuple[str, int]] = None, 
    app: str = "web", 
    use_fs_files: bool = False, 
    *, 
    async_: Literal[True], 
    **request_kwargs, 
) -> Coroutine[Any, Any, int]:
    ...
def get_file_count(
    client: str | PathLike | P115Client, 
    cid: int | str = 0, 
    id_to_dirnode: None | EllipsisType | MutableMapping[int, tuple[str, int]] = None, 
    app: str = "web", 
    use_fs_files: bool = False, 
    *, 
    async_: Literal[False, True] = False, 
    **request_kwargs, 
) -> int | Coroutine[Any, Any, int]:
    """获取文件总数

    .. caution::
        如果 ``use_fs_files = True``，但 ``cid`` 不存在、已经被删除或者是文件，那么相当于 ``cid = 0``，这会导致导致长久的等待

    :param client: 115 客户端或 cookies
    :param cid: 目录 id 或 pickcode
    :param id_to_dirnode: 字典，保存 id 到对应文件的 ``(name, parent_id)`` 元组的字典
    :param app: 使用指定 app（设备）的接口
    :param use_fs_files: 使用 `client.fs_files`，否则使用 `client.fs_category_get`
    :param async_: 是否异步
    :param request_kwargs: 其它请求参数

    :return: 目录内的文件总数（不包括目录）
    """
    if isinstance(client, (str, PathLike)):
        client = P115Client(client, check_for_relogin=True)
    def gen_step(cid: int = to_id(cid), /):
        if not cid:
            resp = yield client.fs_space_summury(async_=async_, **request_kwargs)
            check_response(resp)
            return sum(v["count"] for k, v in resp["type_summury"].items() if k.isupper())
        elif use_fs_files:
            with with_iter_next(iter_list(
                client, 
                cid, 
                page_size=1, 
                payload={"hide_data": 1, "show_dir": 0}, 
                normalize_attr=None, 
                id_to_dirnode=id_to_dirnode, 
                app=app, 
                async_=async_, 
                **request_kwargs, 
            )) as get_next:
                resp = yield get_next()
            return int(resp["count"])
        else:
            resp = yield get_info(
                client, 
                cid, 
                id_to_dirnode=id_to_dirnode, 
                app=app, 
                async_=async_, 
                **request_kwargs, 
            )
            if resp["sha1"]:
                resp["cid"] = cid
                raise NotADirectoryError(errno.ENOTDIR, resp)
            return int(resp["count"])
    return run_gen_step(gen_step, async_)

