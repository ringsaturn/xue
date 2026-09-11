import { beforeEach, describe, expect, it } from "vitest";

import { LOCALES } from "../../web/src/i18n";
import { alternateUrls, applyPageMeta, canonicalUrl, pageUrl } from "../../web/src/pagemeta";
import { SITE_ORIGIN } from "../../web/src/site";

describe("page URLs", () => {
  it("names a page, optionally pinned to a language", () => {
    expect(pageUrl("/", null)).toBe(`${SITE_ORIGIN}/`);
    expect(pageUrl("/", "zh")).toBe(`${SITE_ORIGIN}/?lang=zh`);
    expect(pageUrl("/?case=zhengzhou-2021", "zh-Hant")).toBe(`${SITE_ORIGIN}/?case=zhengzhou-2021&lang=zh-Hant`);
    expect(pageUrl("/showcase.html", "ja")).toBe(`${SITE_ORIGIN}/showcase.html?lang=ja`);
  });

  it("declares every locale plus x-default as an alternate", () => {
    const alternates = alternateUrls("/");
    expect(alternates).toHaveLength(LOCALES.length + 1);
    expect(alternates.map((entry) => entry.hreflang)).toEqual([
      "zh-CN",
      "zh-TW",
      "en",
      "ja",
      "ko",
      "de",
      "fr",
      "es",
      "pt",
      "ru",
      "x-default",
    ]);
    expect(alternates.at(-1)).toEqual({ hreflang: "x-default", href: `${SITE_ORIGIN}/` });
    expect(alternates[1]).toEqual({ hreflang: "zh-TW", href: `${SITE_ORIGIN}/?lang=zh-Hant` });
  });

  it("makes a language-pinned rendering canonical to itself, and view state fold into the page", () => {
    expect(canonicalUrl("/", "?model=ecmwf&type=wind")).toBe(`${SITE_ORIGIN}/`);
    expect(canonicalUrl("/", "?lang=zh-TW&type=temp")).toBe(`${SITE_ORIGIN}/?lang=zh-Hant`);
    expect(canonicalUrl("/", "?lang=klingon")).toBe(`${SITE_ORIGIN}/`);
    expect(canonicalUrl("/?case=x", "?case=x&lang=de")).toBe(`${SITE_ORIGIN}/?case=x&lang=de`);
  });
});

describe("applyPageMeta", () => {
  beforeEach(() => {
    document.head.innerHTML = `
      <title>Fallback</title>
      <meta name="description" content="old" />
      <link rel="canonical" href="${SITE_ORIGIN}/" />
      <link rel="alternate" hreflang="en" href="${SITE_ORIGIN}/?lang=en" />
      <meta property="og:url" content="${SITE_ORIGIN}/" />
      <meta property="og:title" content="Fallback" />
      <meta property="og:description" content="old" />
      <meta name="twitter:title" content="Fallback" />
      <meta name="twitter:description" content="old" />
    `;
  });

  function href(selector: string): string | null {
    return document.head.querySelector<HTMLLinkElement>(selector)?.getAttribute("href") ?? null;
  }

  it("rewrites the canonical, og:url and the whole hreflang set for a case", () => {
    applyPageMeta({ path: "/?case=shadel-2026" }, "?case=shadel-2026&type=radar");
    expect(href('link[rel="canonical"]')).toBe(`${SITE_ORIGIN}/?case=shadel-2026`);
    expect(document.head.querySelector('meta[property="og:url"]')?.getAttribute("content")).toBe(
      `${SITE_ORIGIN}/?case=shadel-2026`,
    );
    // The one alternate the markup shipped is updated; the rest are added.
    expect(href('link[rel="alternate"][hreflang="en"]')).toBe(`${SITE_ORIGIN}/?case=shadel-2026&lang=en`);
    expect(href('link[rel="alternate"][hreflang="zh-CN"]')).toBe(`${SITE_ORIGIN}/?case=shadel-2026&lang=zh`);
    expect(href('link[rel="alternate"][hreflang="x-default"]')).toBe(`${SITE_ORIGIN}/?case=shadel-2026`);
    expect(document.head.querySelectorAll('link[rel="alternate"][hreflang]')).toHaveLength(LOCALES.length + 1);
    // Untouched when not given.
    expect(document.title).toBe("Fallback");
    expect(document.head.querySelector('meta[name="description"]')?.getAttribute("content")).toBe("old");
  });

  it("sets the title with the site suffix and the description on every card", () => {
    applyPageMeta({ path: "/", title: "Surface Temperature · GFS 240H", description: "Two-metre temperature." }, "");
    expect(document.title).toBe("Surface Temperature · GFS 240H · Xue");
    for (const selector of ['meta[property="og:title"]', 'meta[name="twitter:title"]']) {
      expect(document.head.querySelector(selector)?.getAttribute("content")).toBe("Surface Temperature · GFS 240H · Xue");
    }
    for (const selector of ['meta[name="description"]', 'meta[property="og:description"]', 'meta[name="twitter:description"]']) {
      expect(document.head.querySelector(selector)?.getAttribute("content")).toBe("Two-metre temperature.");
    }
  });

  it("creates the canonical link when the markup has none", () => {
    document.head.querySelector('link[rel="canonical"]')?.remove();
    applyPageMeta({ path: "/showcase.html" }, "?lang=fr");
    expect(href('link[rel="canonical"]')).toBe(`${SITE_ORIGIN}/showcase.html?lang=fr`);
  });
});
