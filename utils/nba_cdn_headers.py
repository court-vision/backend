"""Browser-shaped request headers for NBA static CDN hosts.

A plain ``requests.get`` against ``cdn.nba.com`` is answered with 403 by the
CDN's bot check; this exact header set is accepted. Keep the values verbatim.

Usage:
    from utils.nba_cdn_headers import NBA_CDN_HEADERS
    requests.get(url, headers=NBA_CDN_HEADERS("cdn.nba.com"))
"""


def NBA_CDN_HEADERS(host: str = "cdn.nba.com") -> dict[str, str]:
    """Return the header dict, with ``Host`` set to ``host``.

    Note ``Accept-Encoding`` advertises ``br``; if the CDN answers with brotli
    the ``brotli`` package must be installed for ``requests`` to decode it.
    """
    return {
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "Host": host,
        "Origin": "https://www.nba.com",
        "Pragma": "no-cache",
        "Referer": "https://www.nba.com/",
        "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
    }
