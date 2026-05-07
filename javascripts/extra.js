/* Sidebar nav: open external link entries in a new window.
 *
 * The /URI annotations extracted from attachment PDFs are listed as nav
 * children of each attachment via awesome-pages, which renders them as
 * plain <a href="..."> tags — no target attribute, so the host site
 * navigates away when clicked. We want them to open separately, the
 * same way the body-content "Open PDF in new window" link does.
 *
 * We hook into Material's `document$` observable when present so this
 * runs on every page change under `navigation.instant`; otherwise we
 * fall back to a one-shot DOMContentLoaded pass.
 */
(function () {
  function annotateExternalNavLinks() {
    var links = document.querySelectorAll(".md-nav__link");
    for (var i = 0; i < links.length; i++) {
      var href = links[i].getAttribute("href") || "";
      if (/^(https?:|mailto:)/i.test(href)) {
        links[i].setAttribute("target", "_blank");
        links[i].setAttribute("rel", "noopener");
      }
    }
  }

  if (typeof document$ !== "undefined" && document$.subscribe) {
    document$.subscribe(annotateExternalNavLinks);
  } else {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", annotateExternalNavLinks);
    } else {
      annotateExternalNavLinks();
    }
  }
})();
