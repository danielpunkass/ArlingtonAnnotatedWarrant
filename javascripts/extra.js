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
  function isExternalHref(href) {
    if (/^mailto:/i.test(href)) return true;
    if (!/^https?:/i.test(href)) return false;
    // Mkdocs serve and some build configs render internal nav links as
    // absolute URLs (e.g. http://localhost:8000/Article-02/) that match
    // the http(s) prefix. Only mark a link external when its host differs
    // from the host of the current page.
    try {
      return new URL(href, window.location.href).host !== window.location.host;
    } catch (e) {
      return false;
    }
  }

  function annotateExternalNavLinks() {
    var links = document.querySelectorAll(".md-nav__link");
    for (var i = 0; i < links.length; i++) {
      var href = links[i].getAttribute("href") || "";
      if (isExternalHref(href)) {
        links[i].setAttribute("target", "_blank");
        links[i].setAttribute("rel", "noopener");
      }
    }
  }

  /* Click-to-toggle on section labels.
   *
   * The chevron-toggle <label> is hidden via extra.css (we use
   * indentation alone to communicate nesting). To still let users
   * expand/collapse a section, we hijack clicks on the section's
   * link <a> and flip the associated checkbox. Material's instant
   * nav still handles the link's href on the same click. Children
   * appear only when their direct parent is clicked — we don't
   * recurse, so URI links sit collapsed under their attachment until
   * the attachment itself is opened. */
  document.addEventListener(
    "click",
    function (event) {
      var link = event.target.closest(".md-nav__container > a.md-nav__link");
      if (!link) return;
      var container = link.parentElement;
      var item = container.parentElement;
      if (!item || !item.classList.contains("md-nav__item--nested")) return;
      var toggle = null;
      for (var c = item.firstElementChild; c; c = c.nextElementSibling) {
        if (c.tagName === "INPUT" && c.classList.contains("md-nav__toggle")) {
          toggle = c;
          break;
        }
      }
      if (!toggle) return;
      toggle.checked = !toggle.checked;
    },
    true,
  );

  /* Preserve the sidebar's scroll position across instant navigation.
   *
   * Material auto-scrolls the primary sidebar so the link for the
   * current page lands near the top of the viewport. With
   * `navigation.indexes`, clicking an article label both navigates
   * (so Material scrolls to that link) and expands the section (so
   * children appear below it). The combination scrolls the nav so
   * the children sit where the parent label was — the user's cursor
   * ends up hovering over a freshly-revealed child instead of the
   * label they clicked. We cancel that by snapshotting the scroll
   * position on click and restoring it on the next document$ tick
   * (after Material has finished its own positioning). */
  function findNavScrollwrap() {
    return document.querySelector(".md-sidebar--primary .md-sidebar__scrollwrap");
  }

  var pendingScrollTop = null;
  document.addEventListener(
    "click",
    function (event) {
      if (!event.target.closest(".md-nav__link")) return;
      var nav = findNavScrollwrap();
      if (nav) pendingScrollTop = nav.scrollTop;
    },
    true,
  );

  function restoreNavScroll() {
    if (pendingScrollTop === null) return;
    var nav = findNavScrollwrap();
    var target = pendingScrollTop;
    pendingScrollTop = null;
    if (!nav) return;
    // Two rAFs: first lets Material do its scroll, second is our fix.
    requestAnimationFrame(function () {
      requestAnimationFrame(function () {
        nav.scrollTop = target;
      });
    });
  }

  /* Scale inlined PDF renders down to fit narrow viewports.
   *
   * pdf2htmlEX produces pages at fixed pixel dimensions (8.5"×11" at
   * --zoom 1.5 ≈ 1224×1584px). On phones the container is ~375px,
   * so the page would overflow horizontally. We measure the page's
   * natural width and apply CSS `zoom` to .pdf-rendered when the
   * container is smaller — `zoom` is non-standard but well-supported
   * in Chrome/Safari/Firefox and is the only thing that scales both
   * the visual size *and* the layout box (transform: scale doesn't
   * shrink the bounding box). On wide viewports we leave zoom unset
   * and let .pdf-rendered's overflow-x: auto take over for any case
   * where the natural width still exceeds the available space. */
  function fitPdfRendersToWidth() {
    var wraps = document.querySelectorAll(".pdf-rendered");
    for (var i = 0; i < wraps.length; i++) {
      var wrap = wraps[i];
      wrap.style.zoom = "";
      var pages = wrap.querySelectorAll(".pf");
      if (!pages.length) continue;
      var natural = pages[0].offsetWidth;
      if (!natural) continue;
      var available = wrap.parentElement.clientWidth;
      if (available >= natural) continue;
      wrap.style.zoom = (available / natural).toFixed(3);
    }
  }

  var resizeTimer = null;
  window.addEventListener("resize", function () {
    if (resizeTimer) clearTimeout(resizeTimer);
    resizeTimer = setTimeout(fitPdfRendersToWidth, 100);
  });

  if (typeof document$ !== "undefined" && document$.subscribe) {
    document$.subscribe(function () {
      annotateExternalNavLinks();
      restoreNavScroll();
      fitPdfRendersToWidth();
    });
  } else {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", function () {
        annotateExternalNavLinks();
        fitPdfRendersToWidth();
      });
    } else {
      annotateExternalNavLinks();
      fitPdfRendersToWidth();
    }
  }
})();
