import type { Metadata } from "next";
import "./globals.css";
import { resolveSiteUrl } from "@/lib/site";

const OG_TITLE = "FinSight — Automated Equity Research";
// Filings supply the financials and the DCF, and nothing else here: sentiment
// scores earnings-call text, and today every score is of a labelled placeholder.
const OG_DESCRIPTION =
  "Cited financials and a DCF valuation from SEC 10-K data. Sentiment is scored on earnings-call text, not filings; the bundled demo's is a labelled synthetic placeholder.";

const SITE_URL = resolveSiteUrl({
  NEXT_PUBLIC_SITE_URL: process.env.NEXT_PUBLIC_SITE_URL,
  NODE_ENV: process.env.NODE_ENV,
});
// Share-card images need an absolute URL. Without a site URL, Next.js would
// resolve them against http://localhost:3000, so they are left out instead.
const OG_IMAGES = SITE_URL
  ? [{ url: "/og.png", width: 1200, height: 630, alt: "FinSight equity research dashboard" }]
  : undefined;

export const metadata: Metadata = {
  ...(SITE_URL ? { metadataBase: SITE_URL } : {}),
  title: {
    default: OG_TITLE,
    template: "%s · FinSight",
  },
  description:
    "Equity research from SEC EDGAR 10-K data: cited XBRL financials and a DCF valuation, computed by a Python engine. Sentiment is scored on earnings-call text, not filings; the bundled demo's is a labelled synthetic placeholder.",
  applicationName: "FinSight",
  authors: [{ name: "Rohan Khiani", url: "https://github.com/ROROK20" }],
  keywords: [
    "equity research",
    "DCF",
    "SEC EDGAR",
    "financial analysis",
    "sentiment analysis",
    "valuation",
  ],
  icons: { icon: "/icon.svg" },
  openGraph: {
    title: OG_TITLE,
    description: OG_DESCRIPTION,
    url: "https://github.com/ROROK20/FinSight",
    siteName: "FinSight",
    type: "website",
    images: OG_IMAGES,
  },
  twitter: {
    card: "summary_large_image",
    title: OG_TITLE,
    description: OG_DESCRIPTION,
    images: SITE_URL ? ["/og.png"] : undefined,
  },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="font-mono antialiased">{children}</body>
    </html>
  );
}
