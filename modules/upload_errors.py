"""Safe network diagnostics: never expose request URLs, headers or bodies."""
import re


def curl_error_code(error):
    match = re.search(r'curl:\s*\((\d+)\)', str(error))
    return int(match.group(1)) if match else None


def safe_upload_error(error):
    code = curl_error_code(error)
    if code == 60:
        return 'TLS 证书验证失败（curl=60）；请检查系统时间、证书链和信任配置，不自动重试'
    if code == 28 or isinstance(error, TimeoutError):
        return '网络超时（curl=28）；若发生于最终投稿，结果未知，请先核查稿件，勿重复提交'
    if code is not None:
        return f'网络请求失败（curl={code}）'
    return f'上传请求失败（{type(error).__name__}）'


def retryable_chunk_error(error, status_code):
    code = curl_error_code(error)
    if code is not None:
        return code in {6, 7, 18, 28, 35, 52, 55, 56}
    if status_code is not None:
        return status_code in {408, 429, 500, 502, 503, 504}
    return isinstance(error, (TimeoutError, ConnectionError, OSError))
