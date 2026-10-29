"""Bounded HTTPS and Responses SSE for ChatGPT plan usage; no exchange tools."""
import http.client
import json
import ssl
import time
from urllib.parse import urlsplit


class ChatGPTError(ValueError):
    def __init__(self, message, code='CHATGPT', status=None):
        super().__init__(message)
        self.code, self.status = code, status


def provider_error(status=None, code=None):
    messages = {
        'subscription_sharing_usage_limit_exceeded': 'ChatGPT 套餐或本应用额度已用完，请到 ChatGPT 管理用量。',
        'subscription_sharing_usage_unavailable': '当前账户暂不能使用 ChatGPT 套餐，请检查套餐授权与用量设置。',
        'invalid_grant': '登录凭证已失效，请重新登录 ChatGPT。',
        'access_denied': '未完成 ChatGPT 登录或套餐授权。',
    }
    if isinstance(code, str) and code in messages:
        return ChatGPTError(messages[code], code, status)
    message = {400: 'ChatGPT 拒绝请求参数或当前模型不支持所选功能；可关闭联网后重新发送。',
               401: 'ChatGPT 登录已失效，请重新登录。',
               403: '当前账户或工作区没有此功能权限，请检查 ChatGPT 授权。',
               404: '所选模型不可用，请刷新账户模型列表。',
               429: 'ChatGPT 额度不足或请求过于频繁，请查看用量后再试。'}.get(status, 'ChatGPT 请求未完成；不会自动重复发送。')
    return ChatGPTError(message, 'HTTP' if status else 'RESPONSE', status)


def _connection(url, timeout):
    p = urlsplit(url)
    if (p.scheme != 'https' or p.hostname not in ('auth.openai.com', 'api.openai.com')
            or p.username or p.password or p.port not in (None, 443) or p.fragment):
        raise ChatGPTError('ChatGPT 服务地址校验失败。')
    c = http.client.HTTPSConnection(p.hostname, timeout=timeout, context=ssl.create_default_context())
    return c, p.path + ('?' + p.query if p.query else '')


def request_json(url, method='GET', body=None, headers=None):
    """No proxies, redirects, automatic retries or raw provider error logging."""
    c, path = _connection(url, 30)
    try:
        c.request(method, path, body=body, headers={'Accept': 'application/json', **(headers or {})})
        r = c.getresponse()
        raw = r.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ChatGPTError('ChatGPT 响应超过大小限制。')
        try:
            value = json.loads(raw) if raw else {}
        except (ValueError, UnicodeError):
            raise provider_error(r.status) from None
        if r.status != 200:
            e = value.get('error') if isinstance(value, dict) else None
            raise provider_error(r.status, e.get('code') if isinstance(e, dict) else e)
        if not isinstance(value, dict):
            raise ChatGPTError('ChatGPT 响应格式无效。')
        return value
    except (OSError, http.client.HTTPException):
        raise ChatGPTError('无法连接 ChatGPT，请检查网络后重试。', 'NETWORK') from None
    finally:
        c.close()


def parse_sse(lines, on_delta, cancelled):
    """Only response.completed commits a result; partial text stays provisional."""
    data, size, delta_size = [], 0, 0
    completed_items = {}
    for line in lines:
        if cancelled.is_set():
            raise ChatGPTError('分析已取消。', 'CANCELLED')
        size += len(line)
        if size > 2 * 1024 * 1024:
            raise ChatGPTError('模型响应过大，已停止接收。', 'TOO_LARGE')
        if line.strip():
            if line.startswith(b'data:'):
                data.append(line[5:].strip())
            continue
        if not data:
            continue
        raw, data = b'\n'.join(data), []
        if raw == b'[DONE]':
            break
        try:
            event = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ChatGPTError('ChatGPT 流式响应格式异常。') from None
        if not isinstance(event, dict):
            raise ChatGPTError('ChatGPT 流式响应格式异常。')
        kind = event.get('type')
        if kind == 'response.output_text.delta':
            delta = event.get('delta')
            if not isinstance(delta, str):
                raise ChatGPTError('ChatGPT 返回了无效文本。')
            delta_size += len(delta)
            if delta_size > 60000:
                raise ChatGPTError('分析内容超过本次显示上限，已停止接收。', 'TOO_LARGE')
            on_delta(delta)
        elif kind == 'response.output_item.done':
            item, index = event.get('item'), event.get('output_index')
            if isinstance(item, dict) and item.get('type') == 'message':
                if type(index) is not int or not 0 <= index < 100:
                    raise ChatGPTError('ChatGPT 消息顺序无效。')
                completed_items[index] = item
        elif kind == 'response.completed':
            response = event.get('response', {})
            if response.get('status') != 'completed' or response.get('error'):
                raise ChatGPTError('ChatGPT 未确认完整完成。', 'INCOMPLETE')
            texts, citations = [], []
            outputs = response.get('output', [])
            # Plan-usage streams may leave terminal output empty: the complete
            # messages (including citations) arrived in output_item.done.
            # These are committed only after this successful terminal event.
            if not outputs:
                outputs = [completed_items[i] for i in sorted(completed_items)]
            for output in outputs:
                if output.get('type') != 'message':
                    continue
                for part in output.get('content', []):
                    if part.get('type') == 'output_text' and isinstance(part.get('text'), str):
                        texts.append(part['text'])
                        for a in part.get('annotations', []):
                            if a.get('type') == 'url_citation':
                                p = urlsplit(a.get('url', ''))
                                if p.scheme == 'https' and p.hostname and not p.username and not p.password:
                                    citations.append(dict(url=a['url'][:2048], title=str(a.get('title', '来源'))[:250]))
            text = '\n'.join(texts)
            if not text or len(text) > 60000:
                raise ChatGPTError('ChatGPT 未返回完整可显示的分析。', 'INCOMPLETE')
            usage = response.get('usage') or {}
            return dict(text=text, citations=citations[:40], model=str(response.get('model', ''))[:160],
                        usage={k: usage[k] for k in ('input_tokens', 'output_tokens', 'total_tokens')
                               if type(usage.get(k)) is int and usage[k] >= 0})
        elif kind in ('response.failed', 'error'):
            error = event.get('response', {}).get('error') or event.get('error') or event
            raise provider_error(code=error.get('code') if isinstance(error, dict) else None)
        elif kind == 'response.incomplete':
            raise ChatGPTError('ChatGPT 回答被截断；部分内容不作为完整分析。', 'INCOMPLETE')
    raise ChatGPTError('连接结束但未收到完成确认；部分内容不作为完整分析。', 'INCOMPLETE')


def stream_response(access_token, payload, on_delta, cancelled):
    c, path = _connection('https://api.openai.com/v1/responses', 45)
    deadline = time.monotonic() + 180
    try:
        c.request('POST', path, body=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode(),
                  headers={'Authorization': 'Bearer ' + access_token, 'Content-Type': 'application/json',
                           'Accept': 'text/event-stream', 'Accept-Encoding': 'identity'})
        r = c.getresponse()
        if r.status != 200:
            # Error strings may contain request inputs. Expose only a fixed mapping.
            raw = r.read(65537)
            try:
                value = json.loads(raw) if len(raw) <= 65536 else {}
                code = (value.get('error') or {}).get('code')
            except (ValueError, AttributeError):
                code = None
            raise provider_error(r.status, code)
        content_type = (r.getheader('Content-Type') or '').split(';', 1)[0].strip().lower()
        # The ChatGPT plan route can return a valid SSE body without a
        # Content-Type header. Still require a valid response.completed event;
        # missing headers must not turn HTML, JSON or partial text into success.
        if content_type and content_type != 'text/event-stream':
            raise ChatGPTError('ChatGPT 未返回所需的流式响应。')

        def lines():
            while True:
                if cancelled.is_set():
                    raise ChatGPTError('分析已取消。', 'CANCELLED')
                if time.monotonic() >= deadline:
                    raise ChatGPTError('分析超过三分钟，已停止等待；未自动重试。', 'TIMEOUT')
                line = r.readline(262145)
                if len(line) > 262144:
                    raise ChatGPTError('ChatGPT 单条响应过大。')
                if not line:
                    break
                yield line
        return parse_sse(lines(), on_delta, cancelled)
    except (OSError, http.client.HTTPException):
        raise ChatGPTError('分析连接中断；未自动重试，已收到的内容仅供查看。', 'NETWORK') from None
    finally:
        c.close()
