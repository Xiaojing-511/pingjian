"""邮箱验证码登录。

浏览器只提交邮箱和验证码。本服务调用 CloudBase：
先发验证码，再校验，已注册用户登录，未注册用户用同一枚验证凭证注册。
验证码大约 10 分钟有效。登录后的 Access Token 大约 2 小时，
用 Refresh Token 续期，默认可维持约 30 天，不必每天重新收验证码。
Access Token 只用来解析 uid。数据库写入用的是服务端 API Key，不是这枚用户令牌。
"""

from __future__ import annotations

from typing import Protocol

import httpx


class AuthError(Exception):
    pass


class Auth(Protocol):
    def send_email_code(self, email: str) -> dict: ...

    def complete_email(self, email: str, verification_id: str, code: str, is_user: bool) -> dict: ...

    def refresh_session(self, refresh_token: str) -> dict: ...

    def user_from_token(self, token: str) -> dict: ...


class CloudBaseAuth:
    def __init__(self, env_id: str):
        self.env_id = env_id
        self.base = f"https://{env_id}.api.tcloudbasegateway.com"
        self.http = httpx.Client(timeout=30)

    def send_email_code(self, email: str) -> dict:
        data = self._post("/auth/v1/verification", {"email": email, "target": "ANY"})
        verification_id = str(data.get("verification_id") or "")
        if not verification_id:
            raise AuthError("CloudBase 没有返回验证码编号")
        return {
            "verificationId": verification_id,
            "expiresIn": data.get("expires_in") or 600,
            "isUser": _flag(data.get("is_user")),
        }

    def complete_email(self, email: str, verification_id: str, code: str, is_user: bool) -> dict:
        verified = self._post(
            "/auth/v1/verification/verify",
            {"verification_id": verification_id, "verification_code": code},
        )
        verification_token = str(verified.get("verification_token") or "")
        if not verification_token:
            raise AuthError("验证码校验没有返回凭证")
        if is_user:
            data = self._post("/auth/v1/signin", {"verification_token": verification_token})
        else:
            data = self._post(
                "/auth/v1/signup",
                {"email": email, "verification_token": verification_token},
            )
        return self._session(data, email)

    def refresh_session(self, refresh_token: str) -> dict:
        data = self._post(
            "/auth/v1/token",
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self.env_id,
            },
        )
        return self._session(data, "")

    def user_from_token(self, token: str) -> dict:
        response = self.http.get(
            f"{self.base}/auth/v1/user/me",
            headers={"Authorization": f"Bearer {token}", "x-device-id": "pingjian-server"},
        )
        data = _body(response)
        if response.status_code >= 400:
            raise AuthError("登录已过期或无效")
        uid = str(data.get("sub") or data.get("user_id") or "")
        if not uid:
            raise AuthError("无法解析用户身份")
        name = str(data.get("email") or data.get("username") or "")
        return {"uid": uid, "username": name}

    def _session(self, data: dict, email: str) -> dict:
        token = str(data.get("access_token") or "")
        uid = str(data.get("sub") or "")
        if token and not uid:
            profile = self.user_from_token(token)
            uid = profile["uid"]
            email = profile.get("username") or email
        if not token or not uid:
            raise AuthError("CloudBase 没有返回登录身份")
        return {
            "accessToken": token,
            "refreshToken": data.get("refresh_token") or "",
            "expiresIn": data.get("expires_in") or 0,
            "uid": uid,
            "username": email,
        }

    def _post(self, path: str, payload: dict) -> dict:
        try:
            response = self.http.post(
                f"{self.base}{path}",
                headers={"Content-Type": "application/json", "x-device-id": "pingjian-server"},
                json=payload,
            )
        except httpx.HTTPError:
            raise AuthError("无法连接 CloudBase，请稍后重试") from None
        data = _body(response)
        if response.status_code >= 400:
            raise AuthError(_auth_message(data, response.text))
        return data


def _flag(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return bool(value)


def _body(response: httpx.Response) -> dict:
    try:
        data = response.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _auth_message(data: dict, _raw: str) -> str:
    code = str(data.get("error") or data.get("code") or "").lower()
    description = str(data.get("error_description") or data.get("message") or "").strip()
    lowered = f"{code} {description}".lower()
    if "captcha_required" in lowered or "captcha" in lowered:
        return "发送过于频繁，请稍后再获取验证码"
    if "rate_limit" in lowered or "频率" in description:
        return "验证码发送太频繁，请 60 秒后再试"
    if "invalid_grant" in lowered or "refresh_token" in lowered:
        return "登录已过期，请重新登录"
    if (
        "invalid_verification_code" in lowered
        or "verification code" in lowered
        or "验证码错误" in description
        or "验证码不正确" in description
        or "无效的验证码" in description
    ):
        return "验证码不正确"
    if "expired" in lowered or "过期" in description:
        return "验证码已过期，请重新获取"
    if "user_not_found" in lowered or "用户不存在" in description:
        return "这个邮箱还没有注册"
    if "invalid" in lowered and "email" in lowered:
        return "邮箱格式不正确"
    if "disabled" in lowered and ("email" in lowered or "邮箱" in description):
        return "邮箱登录未在 CloudBase 控制台开启"
    if "already" in lowered and ("exist" in lowered or "register" in lowered):
        return "这个邮箱已经注册，请重新获取验证码后再登录"
    if "%!(" in description or "rpc error" in lowered:
        return "认证失败，请稍后重试"
    return description or "认证失败"
