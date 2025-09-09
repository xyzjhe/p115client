#!/usr/bin/env python3
# encoding: utf-8

__author__ = "ChenyangGao <https://chenyanggao.github.io>"
__all__ = [
    "posix_escape_name", "reduce_image_url_layers", "share_extract_payload", 
    "unescape_115_charref", "determine_part_size", "to_cdn_url", 
    "is_valid_id", "is_valid_sha1", "is_valid_name", "is_valid_pickcode", 
]
__doc__ = "这个模块提供了一些工具函数"

from re import compile as re_compile
from string import digits, hexdigits
from typing import cast, Final, NotRequired, TypedDict
from urllib.parse import parse_qsl, urlsplit

from p115pickcode import is_valid_pickcode
from yarl import URL


CRE_115_CHARREF_sub: Final = re_compile("\\[\x02([0-9]+)\\]").sub
CRE_SHARE_LINK_search = re_compile(r"(?:^|(?<=/))(?P<share_code>[a-z0-9]+)(?:-|\?password=|\?)(?P<receive_code>[a-z0-9]{4})(?!==)\b").search


class SharePayload(TypedDict):
    share_code: str
    receive_code: NotRequired[None | str]


def posix_escape_name(name: str, /, repl: str = "|") -> str:
    """把文件名中的 "/" 转换为另一个字符（默认为 "|"）

    :param name: 文件名
    :param repl: 替换为的目标字符

    :return: 替换后的名字
    """
    return name.replace("/", repl)


def reduce_image_url_layers(url: str, /, size: str | int = "") -> str:
    """从图片的缩略图链接中提取信息，以减少一次 302 访问
    """
    if not url.startswith(("http://thumb.115.com/", "https://thumb.115.com/")):
        return url
    urlp = urlsplit(url)
    sha1, _, size0 = urlp.path.rsplit("/")[-1].partition("_")
    if size == "":
        size = size0 or "0"
    return f"https://imgjump.115.com/?sha1={sha1}&{urlp.query}&size={size}"


def share_extract_payload(link: str, /) -> SharePayload:
    """从链接中提取 share_code 和 receive_code
    """
    link = link.strip("/")
    if link.isalnum():
        return SharePayload(share_code=link)
    elif m := CRE_SHARE_LINK_search(link):
        return cast(SharePayload, m.groupdict())
    urlp = urlsplit(link)
    if urlp.path:
        payload = SharePayload(share_code=urlp.path.rstrip("/").rpartition("/")[-1])
        if urlp.query:
            for k, v in parse_qsl(urlp.query):
                if k == "password":
                    payload["receive_code"] = v
                    break
        return payload
    else:
        raise ValueError("can't extract share_code for {link!r}")


def unescape_115_charref(s: str, /) -> str:
    """对 115 的字符引用进行解码

    :example:

        .. code:: python

            unescape_115_charref("[\x02128074]0号：优质资源") == "👊0号：优质资源"
    """
    return CRE_115_CHARREF_sub(lambda a: chr(int(a[1])), s)


def determine_part_size(
    size: int, 
    min_part_size: int = 1024 * 1024 * 10, 
    max_part_count: int = 10 ** 4, 
) -> int:
    """确定分片上传（multipart upload）时的分片大小

    :param size: 数据大小
    :param min_part_size:  用户期望的分片大小
    :param max_part_count: 最大的分片个数

    :return: 分片大小
    """
    if size <= min_part_size:
        return size
    n = -(-size // max_part_count)
    part_size = min_part_size
    while part_size < n:
        part_size <<= 1
    return part_size


def to_cdn_url(
    url: str, 
    /, 
    host: str = "115cdn.com", 
) -> str:
    """尝试把 ``url`` 转换为特定 CDN 域名下的链接，如果不能转换，则原样输出

    :param url: 待转换的链接
    :param host: 域名，比如可取 "115cdn.com" 或 "https://115vod.com"

    :return: 转换后的链接
    """
    urlp = URL(url)
    original_host = urlp.host
    if original_host == "115.com":
        return str(urlp.with_host(host).with_path("/site" + urlp.path))
    elif not original_host or not original_host.endswith(".115.com") or len(original_host.split(".", 3)) > 3:
        return url
    prefix = original_host.partition(".")[0]
    if not prefix or prefix == "proapi":
        return url
    return str(urlp.with_host(host).with_path(prefix + urlp.path))


def is_valid_id(id: int | str, /) -> bool:
    if isinstance(id, int):
        return id >= 0
    if id == "0":
        return True
    return len(id) > 0 and not (id.startswith("0") or id.strip(digits))


def is_valid_sha1(sha1: str, /) -> bool:
    return len(sha1) == 40 and not sha1.strip(hexdigits)


def is_valid_name(name: str, /) -> bool:
    return not (">" in name or "/" in name)

