import { useEffect, useState, type FormEvent } from "react"
import { ApiError, completeEmail, sendEmailCode, setSession } from "../api"

export function LoginCard({
  notice,
  onSuccess,
}: {
  notice: string
  onSuccess: (username: string) => void
}) {
  const [email, setEmail] = useState("")
  const [code, setCode] = useState("")
  const [verificationId, setVerificationId] = useState("")
  const [isUser, setIsUser] = useState(false)
  const [error, setError] = useState("")
  const [sending, setSending] = useState(false)
  const [pending, setPending] = useState(false)
  const [cooldown, setCooldown] = useState(0)

  useEffect(() => {
    if (cooldown <= 0) return
    const timer = window.setTimeout(() => setCooldown((value) => value - 1), 1000)
    return () => window.clearTimeout(timer)
  }, [cooldown])

  function changeEmail(value: string) {
    setEmail(value)
    setVerificationId("")
    setCode("")
    setError("")
  }

  async function requestCode() {
    const address = email.trim()
    if (!address.includes("@") || !address.includes(".")) {
      setError("请填写有效邮箱")
      return
    }
    setSending(true)
    setError("")
    try {
      const result = await sendEmailCode(address)
      setVerificationId(result.verificationId)
      setIsUser(result.isUser)
      setCooldown(60)
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "验证码发送失败")
    } finally {
      setSending(false)
    }
  }

  async function submit(event: FormEvent) {
    event.preventDefault()
    const address = email.trim()
    if (!verificationId) {
      setError("请先获取验证码")
      return
    }
    if (!/^\d{6}$/.test(code.trim())) {
      setError("请输入 6 位验证码")
      return
    }
    setPending(true)
    setError("")
    try {
      const result = await completeEmail(address, verificationId, code.trim(), isUser)
      const name = result.username || address
      setSession(result.accessToken, name, result.refreshToken ?? "", result.expiresIn ?? 0)
      onSuccess(name)
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "认证失败")
    } finally {
      setPending(false)
    }
  }

  const sent = Boolean(verificationId)

  return (
    <main className="login-page">
      <form className="card login-card" onSubmit={submit}>
        <h1>评见</h1>
        <p className="muted">用邮箱验证码进入自己的评论库。新邮箱验证后会自动注册。</p>
        {notice ? <p className="error banner">{notice}</p> : null}
        <label className="field">
          邮箱
          <span className="inline-field">
            <input
              type="email"
              value={email}
              onChange={(event) => changeEmail(event.target.value)}
              autoComplete="email"
              inputMode="email"
            />
            <button className="secondary" type="button" onClick={requestCode} disabled={sending || cooldown > 0}>
              {sending ? "发送中" : cooldown > 0 ? `${cooldown}s` : "获取验证码"}
            </button>
          </span>
        </label>
        <label className="field">
          验证码
          <input
            value={code}
            onChange={(event) => setCode(event.target.value)}
            inputMode="numeric"
            autoComplete="one-time-code"
            maxLength={6}
            placeholder="6 位数字"
          />
        </label>
        {sent ? (
          <p className="muted hint">
            {isUser ? "这个邮箱已注册，输入验证码即可登录。" : "这个邮箱还没注册，验证后会自动创建账号。"}
          </p>
        ) : null}
        {error ? <p className="error">{error}</p> : null}
        <div className="card-actions" style={{ marginTop: 16 }}>
          <button className="primary" type="submit" disabled={pending}>
            {pending ? "提交中" : sent && !isUser ? "注册并登录" : "登录"}
          </button>
        </div>
      </form>
    </main>
  )
}
