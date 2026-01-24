from curl_cffi import requests
from nba_api.library.http import NBAHTTP

def browser_impersonation_request(self, endpoint, parameters, referer=None, proxy=None, headers=None, timeout=None, raise_exception_on_error=False):
    base_url = self.base_url.format(endpoint=endpoint)
    endpoint = endpoint.lower()
    
    # 1. Header Setup
    request_headers = self.headers.copy()
    if headers:
        request_headers.update(headers)
    if referer:
        request_headers["Referer"] = referer

    # 2. THE FIX: Clean 'None' values
    # Standard requests drops None values automatically. curl_cffi sends them as "None".
    # We must filter them out manually.
    clean_params = {k: v for k, v in parameters.items() if v is not None}

    # 3. Send Request with "impersonate"
    response = requests.get(
        base_url,
        params=clean_params,  # <--- Use the cleaned dictionary
        headers=request_headers,
        timeout=30,
        impersonate="chrome110"
    )

    status_code = response.status_code
    contents = response.text
    
    data = self.nba_response(response=contents, status_code=status_code, url=base_url)
    return data

# Apply the patch
NBAHTTP.send_api_request = browser_impersonation_request