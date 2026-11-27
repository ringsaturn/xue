import { beforeEach, describe, expect, it } from "vitest";

import { LOCALES } from "../../web/src/i18n";
import { alternateUrls, applyPageMeta, canonicalUrl, pageUrl } from "../../web/src/pagemeta";
import { SITE_ORIGIN } from "../../web/src/site";

describe("page URLs", () => {
  it("names a page in a language by its prefix, English and the x-default at the root", () => {
    expect(pageUrl("/", null)).toBe(`${SITE_ORIGIN}/`);
    expect(pageUrl("/", "en")).toBe(`${SITE_ORIGIN}/`);
    expect(pageUrl("/", "zh")).toBe(`${SITE_ORIGIN}/zh/`);
    expect(pageUrl("/showcase/zhengzhou-2021", "zh-Hant")).toBe(`${SITE_ORIGIN}/zh-hant/showcase/zhengzhou-2021`);
    expect(pageUrl("/showcase", "ja")).toBe(`${SITE_ORIGIN}/ja/showcase`);
    expect(pageUrl("/typhoon?storm=WP242025", "ko")).toBe(`${SITE_ORIGIN}/ko/typhoon?storm=WP242025`);
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
      "tr",
      "ru",
      "ne",
      "x-default",
    ]);
    expect(alternates.at(-1)).toEqual({ hreflang: "x-default", href: `${SITE_ORIGIN}/` });
    expect(alternates[1]).toEqual({ hreflang: "zh-TW", href: `${SITE_ORIGIN}/zh-hant/` });
    expect(alternates[2]).toEqual({ hreflang: "en", href: `${SITE_ORIGIN}/` });
  });

  it("makes each language's copy canonical to itself", () => {
    expect(canonicalUrl("/", "en")).toBe(`${SITE_ORIGIN}/`);
    expect(canonicalUrl("/", "zh-Hant")).toBe(`${SITE_ORIGIN}/zh-hant/`);
    expect(canonicalUrl("/showcase/x", "de")).toBe(`${SITE_ORIGIN}/de/showcase/x`);
  });

  it("makes a dataset's page canonical to its path", () => {
    expect(canonicalUrl("/hrrr", "en")).toBe(`${SITE_ORIGIN}/hrrr`);
    expect(canonicalUrl("/hrrr", "ja")).toBe(`${SITE_ORIGIN}/ja/hrrr`);
    expect(alternateUrls("/hrrr").at(-1)).toEqual({ hreflang: "x-default", href: `${SITE_ORIGIN}/hrrr` });
  });
});

describe("applyPageMeta", () => {
  beforeEach(() => {
    document.head.innerHTML = `
      <title>Fallback</title>
      <meta name="description" content="old" />
      <link rel="canonical" href="${SITE_ORIGIN}/" />
      <link rel="alternate" hreflang="en" href="${SITE_ORIGIN}/" />
      <meta property="og:locale" content="en_US" />
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

  it("rewrites the canonical, og:url, og:locale and the whole hreflang set for a case", () => {
    applyPageMeta({ path: "/showcase/shadel-2026" }, "ja");
    expect(href('link[rel="canonical"]')).toBe(`${SITE_ORIGIN}/ja/showcase/shadel-2026`);
    expect(document.head.querySelector('meta[property="og:url"]')?.getAttribute("content")).toBe(
      `${SITE_ORIGIN}/ja/showcase/shadel-2026`,
    );
    expect(document.head.querySelector('meta[property="og:locale"]')?.getAttribute("content")).toBe("ja_JP");
    // The one alternate the markup shipped is updated; the rest are added.
    expect(href('link[rel="alternate"][hreflang="en"]')).toBe(`${SITE_ORIGIN}/showcase/shadel-2026`);
    expect(href('link[rel="alternate"][hreflang="zh-CN"]')).toBe(`${SITE_ORIGIN}/zh/showcase/shadel-2026`);
    expect(href('link[rel="alternate"][hreflang="x-default"]')).toBe(`${SITE_ORIGIN}/showcase/shadel-2026`);
    expect(document.head.querySelectorAll('link[rel="alternate"][hreflang]')).toHaveLength(LOCALES.length + 1);
    // Untouched when not given.
    expect(document.title).toBe("Fallback");
    expect(document.head.querySelector('meta[name="description"]')?.getAttribute("content")).toBe("old");
  });

  it("sets the title with the site suffix and the description on every card", () => {
    applyPageMeta({ path: "/", title: "Surface Temperature · GFS 240H", description: "Two-metre temperature." }, "en");
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
    applyPageMeta({ path: "/showcase" }, "fr");
    expect(href('link[rel="canonical"]')).toBe(`${SITE_ORIGIN}/fr/showcase`);
  });

  it("keeps a noindex view canonical to itself and drops its alternates", () => {
    applyPageMeta({ path: "/typhoon" });
    expect(document.head.querySelector('meta[name="robots"]')).toBeNull();
    applyPageMeta({ path: "/typhoon?storm=WP242025", noindex: true }, "ja");
    expect(href('link[rel="canonical"]')).toBe(`${SITE_ORIGIN}/ja/typhoon?storm=WP242025`);
    expect(document.head.querySelector('meta[name="robots"]')?.getAttribute("content")).toBe("noindex, follow");
    expect(document.head.querySelectorAll('link[rel="alternate"][hreflang]')).toHaveLength(0);
  });

  it("overwrites the robots meta the markup ships", () => {
    document.head.insertAdjacentHTML("beforeend", '<meta name="robots" content="index, follow, max-image-preview:large" />');
    applyPageMeta({ path: "/typhoon?storm=WP242025", noindex: true }, "en");
    const robots = document.head.querySelectorAll('meta[name="robots"]');
    expect(robots).toHaveLength(1);
    expect(robots[0]?.getAttribute("content")).toBe("noindex, follow");
  });
});
