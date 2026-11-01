"""LLM provider adapters and orchestration helpers."""
from __future__ import annotations

from .shared import *


def _openai_compatible_text(
    system_prompt: str,
    user_content: str,
    *,
    api_key: str,
    base_url: str,
    model: str,
    label: str = "",
    timeout: int = 120,
    no_proxy: bool = False,
    temperature: float = 0.7,
    max_tokens: int = 4096,
    response_format: dict[str, object] | None = None,
) -> str:
    """通用 OpenAI 兼容接口调用（DeepSeek / Qwen / 其他）。

    no_proxy=True 时临时清除代理环境变量（用于国内 API 直连）。
    """
    import urllib.request as _req
    import urllib.error as _err
    import json as _json

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if response_format:
        payload["response_format"] = response_format
    url = f"{base_url.rstrip('/')}/chat/completions"
    data = _json.dumps(payload, ensure_ascii=False).encode()
    req = _req.Request(url, data=data, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }, method="POST")

    # 国内 API 需要绕过代理直连
    saved_proxy = {}
    if no_proxy:
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            if k in os.environ:
                saved_proxy[k] = os.environ.pop(k)
        # 重建 opener 以确保不使用代理
        opener = _req.build_opener(_req.ProxyHandler({}))
    else:
        opener = None

    try:
        if opener:
            resp = opener.open(req, timeout=timeout)
        else:
            resp = _req.urlopen(req, timeout=timeout)
        body = _json.loads(resp.read())
        resp.close()
    except _err.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        raise RuntimeError(f"{label or model}: HTTP {exc.code} {detail}") from exc
    finally:
        # 恢复代理环境变量
        for k, v in saved_proxy.items():
            os.environ[k] = v

    choices = body.get("choices", [])
    if not choices:
        raise RuntimeError(f"{label or model}: 无返回 choices")
    text = (choices[0].get("message") or {}).get("content", "").strip()
    if not text:
        raise RuntimeError(f"{label or model}: 空文本")
    return text


def _deepseek_text(system_prompt: str, user_content: str, *, temperature: float = 0.7) -> str:
    """DeepSeek API — 用于素材总结和研究。"""
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("缺少 DEEPSEEK_API_KEY")
    return _openai_compatible_text(
        system_prompt, user_content,
        api_key=api_key,
        base_url="https://api.deepseek.com/v1",
        model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
        label="deepseek",
        temperature=temperature,
    )


def _extract_json_fragment(text: str) -> str:
    cleaned = (text or "").strip()
    cleaned = cleaned.replace(TEXT_MARKER_START, "").replace(TEXT_MARKER_END, "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\}|\[.*\])\s*```", cleaned, flags=re.S)
    if fenced:
        return fenced.group(1).strip()
    start = min([idx for idx in (cleaned.find("{"), cleaned.find("[")) if idx != -1], default=-1)
    if start == -1:
        raise ValueError("未找到 JSON 起始位置")
    end_object = cleaned.rfind("}")
    end_array = cleaned.rfind("]")
    end = max(end_object, end_array)
    if end <= start:
        raise ValueError("未找到 JSON 结束位置")
    return cleaned[start : end + 1].strip()


def _parse_json_response(text: str) -> dict[str, object]:
    fragment = _extract_json_fragment(text)
    data = json.loads(fragment)
    if not isinstance(data, dict):
        raise ValueError("JSON 响应不是对象")
    return data


def _openai_compatible_json(
    system_prompt: str,
    user_content: str,
    *,
    api_key: str,
    base_url: str,
    model: str,
    label: str = "",
    timeout: int = 120,
    no_proxy: bool = False,
    temperature: float = 0.2,
    max_tokens: int = 4096,
) -> dict[str, object]:
    text = _openai_compatible_text(
        system_prompt,
        user_content,
        api_key=api_key,
        base_url=base_url,
        model=model,
        label=label,
        timeout=timeout,
        no_proxy=no_proxy,
        temperature=temperature,
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
    )
    return _parse_json_response(text)


def _qwen_text(system_prompt: str, user_content: str, *, temperature: float = 0.7) -> str:
    """Qwen-Turbo API — 最便宜，用于 JSON 解析等低价值场景。"""
    api_key = os.getenv("QWEN_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("缺少 QWEN_API_KEY")
    return _openai_compatible_text(
        system_prompt, user_content,
        api_key=api_key,
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        model=os.getenv("QWEN_MODEL", "qwen-turbo"),
        label="qwen-turbo",
        no_proxy=True,
        temperature=temperature,
    )


def _qwen_plus_text(system_prompt: str, user_content: str, *, temperature: float = 0.7) -> str:
    """Qwen-Plus API — 用于文案润色，润稿质量显著优于 Turbo，价格仍极低
    (¥0.8/¥2 per M tokens)。作为 Gemini 的主要中文兜底。"""
    api_key = os.getenv("QWEN_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("缺少 QWEN_API_KEY")
    return _openai_compatible_text(
        system_prompt, user_content,
        api_key=api_key,
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        model=os.getenv("QWEN_PLUS_MODEL", "qwen-plus"),
        label="qwen-plus",
        no_proxy=True,
        temperature=temperature,
    )


def _deepseek_json(system_prompt: str, user_content: str, *, temperature: float = 0.2) -> dict[str, object]:
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("缺少 DEEPSEEK_API_KEY")
    return _openai_compatible_json(
        system_prompt,
        user_content,
        api_key=api_key,
        base_url="https://api.deepseek.com/v1",
        model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
        label="deepseek",
        temperature=temperature,
    )


def _qwen_json(system_prompt: str, user_content: str, *, temperature: float = 0.2) -> dict[str, object]:
    api_key = os.getenv("QWEN_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("缺少 QWEN_API_KEY")
    return _openai_compatible_json(
        system_prompt,
        user_content,
        api_key=api_key,
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        model=os.getenv("QWEN_MODEL", "qwen-turbo"),
        label="qwen-turbo",
        no_proxy=True,
        temperature=temperature,
    )


def _gemini_text(system_prompt: str, user_content: str, *, temperature: float = 0.7) -> str:
    """调用 Gemini 文本 API 生成文本（备选方案）。
    对 503 UNAVAILABLE / 500 / 504 / 连接错误做退避重试（30s、60s），每个 model 最多 3 次。
    Paid Tier 1 仍经常被 Google 容量池抢占，重试是必要的。
    """
    import urllib.request as _req
    import urllib.error as _err
    import json as _json
    import time as _time
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("缺少 GEMINI_API_KEY。")
    models_raw = os.getenv("GEMINI_TEXT_MODELS", "gemini-2.5-flash")
    models = [model.strip() for model in models_raw.split(",") if model.strip()]
    # 重试等待时间（秒）：首次失败后 30s，再失败 60s
    retry_backoff = [30, 60]
    retriable_http = {500, 502, 503, 504}
    last_error = "unknown"
    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_content}]}],
        "generationConfig": {"temperature": temperature, "maxOutputTokens": 4096},
    }

    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        data = _json.dumps(payload, ensure_ascii=False).encode()
        for attempt in range(1 + len(retry_backoff)):
            req = _req.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
            retriable = False
            try:
                with _req.urlopen(req, timeout=120) as resp:
                    body = _json.loads(resp.read())
            except _err.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                last_error = f"{model}: HTTP {exc.code} {detail}"
                retriable = exc.code in retriable_http
            except (_err.URLError, TimeoutError, ConnectionError) as exc:
                last_error = f"{model}: {exc}"
                retriable = True
            except Exception as exc:
                last_error = f"{model}: {exc}"
                retriable = False
            else:
                candidates = body.get("candidates", [])
                if not candidates:
                    last_error = f"{model}: Gemini 无返回 {body}"
                    break
                parts = candidates[0].get("content", {}).get("parts", [])
                text = "".join(p.get("text", "") for p in parts).strip()
                if text:
                    return text
                last_error = f"{model}: 空文本返回"
                break

            if retriable and attempt < len(retry_backoff):
                wait = retry_backoff[attempt]
                print(f"[gemini] {model} 第{attempt+1}次失败 ({last_error[:80]}...), {wait}s 后重试", file=sys.stderr)
                _time.sleep(wait)
                continue
            break  # 非可重试错误或已用完重试次数

    raise RuntimeError(last_error)


def _gemini_json(system_prompt: str, user_content: str, *, temperature: float = 0.2) -> dict[str, object]:
    text = _gemini_text(
        system_prompt + "\n你必须只输出一个合法 JSON 对象，不要输出解释、Markdown 或多余文字。",
        user_content,
        temperature=temperature,
    )
    return _parse_json_response(text)


def _llm_research(system_prompt: str, user_content: str, *, task: str = "llm_research") -> str:
    """素材总结：DeepSeek → Qwen-Plus → Gemini 降级链。"""
    chain = [(_deepseek_text, "DeepSeek"), (_qwen_plus_text, "Qwen-Plus"), (_gemini_text, "Gemini")]
    chain_names = [name for _, name in chain]
    attempts: list[dict[str, object]] = []
    t_start = time.monotonic()
    for fn, name in chain:
        attempt_start = time.monotonic()
        try:
            result = fn(system_prompt, user_content)
            latency_ms = int((time.monotonic() - attempt_start) * 1000)
            attempts.append({"provider": name, "result": "ok", "latency_ms": latency_ms})
            print(f"[llm] {task} 使用 {name} 成功")
            _daily_log_llm_call(
                task=task,
                chain=chain_names,
                attempts=attempts,
                success_provider=name,
                total_latency_ms=int((time.monotonic() - t_start) * 1000),
                input_chars=len(user_content),
                output_chars=len(result) if isinstance(result, str) else 0,
            )
            return result
        except Exception as exc:
            latency_ms = int((time.monotonic() - attempt_start) * 1000)
            attempts.append({
                "provider": name, "result": "fail",
                "error": str(exc)[:300], "latency_ms": latency_ms,
            })
            print(f"[llm] {name} {task} 失败: {exc}", file=sys.stderr)
    _daily_log_llm_call(
        task=task, chain=chain_names, attempts=attempts,
        success_provider=None,
        total_latency_ms=int((time.monotonic() - t_start) * 1000),
        input_chars=len(user_content),
    )
    raise RuntimeError(f"所有 LLM {task} 均失败")


def _llm_script(system_prompt: str, user_content: str, *, temperature: float = 0.7, task: str = "script_polish") -> str:
    """文案润色：默认 DeepSeek → Qwen-Plus → Gemini。
    Qwen-Turbo 作为最低成本兜底保留在末位，仅当前三家全部失败时使用。"""
    runner_map = {
        "deepseek": (_deepseek_text, "DeepSeek"),
        "qwen-plus": (_qwen_plus_text, "Qwen-Plus"),
        "gemini": (_gemini_text, "Gemini"),
        "qwen": (_qwen_text, "Qwen-Turbo"),
    }
    order_raw = os.getenv("SOLO_SCRIPT_MODEL_ORDER", "deepseek,qwen-plus,gemini,qwen")
    chain: list[tuple[Callable[[str, str], str], str]] = []
    seen: set[str] = set()
    for token in [part.strip().lower() for part in order_raw.split(",") if part.strip()]:
        if token in runner_map and token not in seen:
            chain.append(runner_map[token])
            seen.add(token)
    for token in ("deepseek", "qwen-plus", "gemini", "qwen"):
        if token not in seen:
            chain.append(runner_map[token])
    chain_names = [name for _, name in chain]
    attempts: list[dict[str, object]] = []
    t_start = time.monotonic()
    for fn, name in chain:
        attempt_start = time.monotonic()
        try:
            result = fn(system_prompt, user_content, temperature=temperature)
            latency_ms = int((time.monotonic() - attempt_start) * 1000)
            attempts.append({"provider": name, "result": "ok", "latency_ms": latency_ms})
            print(f"[llm] {task} 使用 {name} 成功")
            _daily_log_llm_call(
                task=task, chain=chain_names, attempts=attempts,
                success_provider=name,
                total_latency_ms=int((time.monotonic() - t_start) * 1000),
                input_chars=len(user_content),
                output_chars=len(result) if isinstance(result, str) else 0,
                temperature=temperature,
            )
            return result
        except Exception as exc:
            latency_ms = int((time.monotonic() - attempt_start) * 1000)
            attempts.append({
                "provider": name, "result": "fail",
                "error": str(exc)[:300], "latency_ms": latency_ms,
            })
            print(f"[llm] {name} {task} 失败: {exc}", file=sys.stderr)
    _daily_log_llm_call(
        task=task, chain=chain_names, attempts=attempts,
        success_provider=None,
        total_latency_ms=int((time.monotonic() - t_start) * 1000),
        input_chars=len(user_content),
        temperature=temperature,
    )
    raise RuntimeError(f"所有 LLM {task} 均失败")


def _llm_json(
    system_prompt: str,
    user_content: str,
    *,
    order_env: str,
    default_order: str,
    label: str,
    temperature: float = 0.2,
) -> dict[str, object]:
    runner_map = {
        "deepseek": (_deepseek_json, "DeepSeek"),
        "qwen": (_qwen_json, "Qwen-Turbo"),
        "gemini": (_gemini_json, "Gemini"),
    }
    order_raw = os.getenv(order_env, default_order)
    chain: list[tuple[object, str]] = []
    seen: set[str] = set()
    for token in [part.strip().lower() for part in order_raw.split(",") if part.strip()]:
        if token in runner_map and token not in seen:
            chain.append(runner_map[token])
            seen.add(token)
    for token in ("deepseek", "qwen", "gemini"):
        if token not in seen:
            chain.append(runner_map[token])
    chain_names = [name for _, name in chain]
    attempts: list[dict[str, object]] = []
    t_start = time.monotonic()
    for fn, name in chain:
        attempt_start = time.monotonic()
        try:
            result = fn(system_prompt, user_content, temperature=temperature)
            latency_ms = int((time.monotonic() - attempt_start) * 1000)
            attempts.append({"provider": name, "result": "ok", "latency_ms": latency_ms})
            print(f"[llm] {label} 使用 {name} 成功")
            _daily_log_llm_call(
                task=label, chain=chain_names, attempts=attempts,
                success_provider=name,
                total_latency_ms=int((time.monotonic() - t_start) * 1000),
                input_chars=len(user_content),
                output_type="json",
                temperature=temperature,
            )
            return result
        except Exception as exc:
            latency_ms = int((time.monotonic() - attempt_start) * 1000)
            attempts.append({
                "provider": name, "result": "fail",
                "error": str(exc)[:300], "latency_ms": latency_ms,
            })
            print(f"[llm] {name} {label} 失败: {exc}", file=sys.stderr)
    _daily_log_llm_call(
        task=label, chain=chain_names, attempts=attempts,
        success_provider=None,
        total_latency_ms=int((time.monotonic() - t_start) * 1000),
        input_chars=len(user_content),
        output_type="json",
        temperature=temperature,
    )
    raise RuntimeError(f"所有 LLM {label} 均失败")


__all__ = [name for name in globals() if not name.startswith("__")]
