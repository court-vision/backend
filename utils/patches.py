"""
NBA API Patches

Patches the nba_api library to use browser impersonation via curl_cffi
to avoid timeout/blocking issues from stats.nba.com.

This module must be imported early in application startup (e.g., in main.py)
to ensure the patch is applied before any nba_api calls are made.
"""

from curl_cffi import requests
from nba_api.library.http import NBAHTTP


# Headers that properly impersonate a browser request to stats.nba.com
NBA_BROWSER_HEADERS = {
    'Host': 'stats.nba.com',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/117.0',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'en-US,en;q=0.5',
    'Referer': 'https://www.nba.com/',
    'Origin': 'https://www.nba.com',
    'Connection': 'keep-alive',
    'x-nba-stats-origin': 'stats',
    'x-nba-stats-token': 'true',
}


def browser_impersonation_request(
    self,
    endpoint,
    parameters,
    referer=None,
    proxy=None,
    headers=None,
    timeout=None,
    raise_exception_on_error=False
):
    """
    Replacement for NBAHTTP.send_api_request that uses curl_cffi
    with browser impersonation to avoid NBA API blocking.
    """
    base_url = self.base_url.format(endpoint=endpoint)
    endpoint = endpoint.lower()

    # Build headers: start with browser headers, then merge any custom ones
    request_headers = NBA_BROWSER_HEADERS.copy()
    if self.headers:
        request_headers.update(self.headers)
    if headers:
        request_headers.update(headers)
    if referer:
        request_headers["Referer"] = referer

    # Clean 'None' values - standard requests drops None values automatically,
    # but curl_cffi sends them as the string "None". Filter them out.
    clean_params = {k: v for k, v in parameters.items() if v is not None}

    # Send request with browser impersonation
    response = requests.get(
        base_url,
        params=clean_params,
        headers=request_headers,
        timeout=timeout or 30,
        impersonate="chrome110"
    )

    status_code = response.status_code
    contents = response.text

    data = self.nba_response(
        response=contents,
        status_code=status_code,
        url=base_url
    )
    return data


def apply_nba_api_patch():
    """Apply the browser impersonation patch to nba_api."""
    NBAHTTP.send_api_request = browser_impersonation_request


# Apply the patch immediately when this module is imported
apply_nba_api_patch()