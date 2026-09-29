interface Env {
  WAITLIST: { get(key: string): Promise<string | null> }
}

export const onRequestGet = async ({ env }: { env: Env }): Promise<Response> => {
  const count = Number((await env.WAITLIST.get("count")) ?? 0)
  return Response.json({ count }, { headers: { "cache-control": "public, max-age=60" } })
}
