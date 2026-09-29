// Cloudflare Pages Function. Storage: KV binding WAITLIST (`email:<addr>` -> ISO time).
// No dependency on the Oracle VM, so signups keep working when it's down.
interface Env {
  WAITLIST: {
    get(key: string): Promise<string | null>
    put(key: string, value: string): Promise<void>
  }
}
type Ctx = { request: Request; env: Env }

const EMAIL = /^[^\s@]{1,64}@[^\s@]{1,255}\.[^\s@]{2,}$/

export const onRequestPost = async ({ request, env }: Ctx): Promise<Response> => {
  let body: { email?: unknown; website?: unknown }
  try {
    body = await request.json()
  } catch {
    return Response.json({ error: "bad json" }, { status: 400 })
  }
  // Honeypot filled => bot. Pretend success so it doesn't retry.
  if (body.website) return Response.json({ ok: true })

  const email = typeof body.email === "string" ? body.email.trim().toLowerCase() : ""
  if (email.length > 254 || !EMAIL.test(email)) {
    return Response.json({ error: "invalid email" }, { status: 400 })
  }

  const key = `email:${email}`
  if (await env.WAITLIST.get(key)) return Response.json({ ok: true }) // dedupe

  await env.WAITLIST.put(key, new Date().toISOString())
  // ponytail: read-modify-write counter, can under-count on simultaneous signups.
  // Fine for a vanity number; use a Durable Object/D1 if it must be exact.
  const n = Number((await env.WAITLIST.get("count")) ?? 0) + 1
  await env.WAITLIST.put("count", String(n))
  return Response.json({ ok: true })
}
