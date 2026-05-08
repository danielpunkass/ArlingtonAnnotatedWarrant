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

  /* Replace the sidebar's primary-nav title (a copy of `site_name`)
   * with a shorter, sidebar-appropriate label. We don't want to change
   * `site_name` itself — that controls the browser tab, the page
   * header, and other places where the full project name still reads
   * better. Find the existing text node inside the .md-nav--primary
   * title label and overwrite its content; the sibling logo <a>
   * stays untouched. */
  function rewriteSidebarTitle() {
    var label = document.querySelector(".md-nav--primary > .md-nav__title");
    if (!label) return;
    for (var i = 0; i < label.childNodes.length; i++) {
      var node = label.childNodes[i];
      if (node.nodeType !== 3) continue;
      var trimmed = node.textContent.trim();
      if (!trimmed || trimmed === "Articles") continue;
      node.textContent = node.textContent.replace(trimmed, "Articles");
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

  /* Disposition icons in the sidebar.
   *
   * Each article in index.json may carry a `disposition` block with
   * a status code. Map the code to the same admonition flavor the
   * summary page uses, and tag the article's nav link with
   * `data-disposition-type`; extra.css renders the color-coded icon
   * on the right edge.
   *
   * Data is fetched once and cached. The DOM walk re-runs on every
   * navigation.instant tick because Material rebuilds nav markup;
   * absent the data attribute, no icon shows. */
  var ADMONITION_TYPE_BY_CODE = {
    y: "success",
    n: "failure",
    t: "warning",
    p: "warning",
    "r/c": "info",
    w: "note",
    "n/a": "note",
  };
  var dispositionsPromise = null;
  function loadDispositions() {
    if (dispositionsPromise) return dispositionsPromise;
    dispositionsPromise = fetch("/index.json")
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        var map = {};
        if (!data || !data.articles) return map;
        for (var i = 0; i < data.articles.length; i++) {
          var a = data.articles[i];
          var d = a && a.disposition;
          if (!d || !d.code) continue;
          var t = ADMONITION_TYPE_BY_CODE[String(d.code).toLowerCase()];
          if (!t) continue;
          // Match the summary page's admonition title verbatim:
          // "<Label> on <Date>" if a date is on record, else just the
          // label. Tooltip and visible heading stay in sync.
          var label = d.label || "Disposed";
          var sentence = d.date ? label + " on " + d.date : label;
          map[a.articleNumber] = { type: t, title: sentence };
        }
        return map;
      })
      .catch(function () { return {}; });
    return dispositionsPromise;
  }

  function annotateDispositionLinks() {
    loadDispositions().then(function (map) {
      var links = document.querySelectorAll(".md-nav__link[href]");
      var re = /(?:^|\/)Article-(\d+)\/?$/;
      for (var i = 0; i < links.length; i++) {
        var href = links[i].getAttribute("href") || "";
        var m = href.match(re);
        // Tag every article landing-page link (regardless of
        // disposition) so CSS can suppress the document icon that
        // would otherwise apply to articles inside the Disposed /
        // Tabled+Postponed groups.
        if (m) {
          links[i].setAttribute("data-article-level", "1");
        } else if (links[i].hasAttribute("data-article-level")) {
          links[i].removeAttribute("data-article-level");
        }
        var entry = m ? map[parseInt(m[1], 10)] : null;
        if (entry) {
          links[i].setAttribute("data-disposition-type", entry.type);
          links[i].setAttribute("title", entry.title);
        } else {
          if (links[i].hasAttribute("data-disposition-type")) {
            links[i].removeAttribute("data-disposition-type");
          }
          // Don't strip arbitrary `title` attributes set elsewhere —
          // only clear the one we manage. Material doesn't set title
          // on these links so a stale title here would be ours.
          if (links[i].hasAttribute("title")) {
            links[i].removeAttribute("title");
          }
        }
      }
    });
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
      rewriteSidebarTitle();
      annotateExternalNavLinks();
      annotateDispositionLinks();
      restoreNavScroll();
      fitPdfRendersToWidth();
    });
  } else {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", function () {
        rewriteSidebarTitle();
        annotateExternalNavLinks();
        annotateDispositionLinks();
        fitPdfRendersToWidth();
      });
    } else {
      rewriteSidebarTitle();
      annotateExternalNavLinks();
      annotateDispositionLinks();
      fitPdfRendersToWidth();
    }
  }
})();
