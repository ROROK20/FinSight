/**
 * The absolute base for OG/Twitter image URLs. NEXT_PUBLIC_SITE_URL is baked
 * in at build time; without it only a development build uses localhost — a
 * production build gets no metadataBase (relative image URLs) rather than
 * advertising http://localhost:3002 in its share cards.
 */
export function resolveSiteUrl(env: { NEXT_PUBLIC_SITE_URL?: string; NODE_ENV?: string }): URL | undefined {
  const configured = env.NEXT_PUBLIC_SITE_URL?.trim();
  if (configured) return new URL(configured);
  return env.NODE_ENV === "development" ? new URL("http://localhost:3002") : undefined;
}
